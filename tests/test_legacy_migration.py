"""Offline pre-PM migration using archived v2026.8.13 source, never live state."""
from pathlib import Path
import shutil
import subprocess
import tarfile
import unittest

from test_runtime import executable, server
import test_runtime as runtime
import addon_runtime


class LegacyMigration(unittest.TestCase):
    home: Path
    code: Path
    launcher: Path
    env: dict
    saved: dict
    setUp = runtime.UpdateRig.setUp
    tearDown = runtime.UpdateRig.tearDown
    run_update = runtime.UpdateRig.run_update
    snapshot = runtime.UpdateRig.snapshot

    def legacy(self):
        shutil.rmtree(self.code / 'pm', ignore_errors=True)
        shutil.rmtree(self.code / '.hermes')
        # These are real tracked source files from the original image revision.
        with tarfile.open(Path(__file__).parent / 'fixtures/legacy-v2026.8.13.tar.gz') as archive:
            for name in ('hermes', 'hermes_cli/main.py', 'pyproject.toml'):
                path = self.code / name
                path.parent.mkdir(parents=True, exist_ok=True)
                member = archive.extractfile(name)
                assert member is not None
                path.write_bytes(member.read())
        subprocess.run(['git', '-C', str(self.code), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.code), 'commit', '-qm', 'old image seed'], check=True)
        self.rev = subprocess.check_output(['git', '-C', str(self.code), 'rev-parse', 'HEAD'], text=True).strip()
        executable(self.code / 'venv/bin/python', '#!/bin/sh\n# Legacy Python 3.11: must not run the modern PM or updater.\nexit 91\n')
        # A local origin with a modern PM revision; no network, credentials or dependencies.
        self.origin = self.home / 'origin'
        subprocess.run(['git', 'clone', '-q', str(self.code), str(self.origin)], check=True)
        for key, value in [('user.email', 'test@example.invalid'), ('user.name', 'Test')]:
            subprocess.run(['git', '-C', str(self.origin), 'config', key, value], check=True)
        (self.origin / 'pm').mkdir()
        (self.origin / 'pm/lock.json').write_text('{"packages":{}}')
        (self.origin / 'pm/cli.py').write_text('''import hashlib, json, os, pathlib, sys
home = pathlib.Path(os.environ['HERMES_HOME'])
if os.environ.get('FAIL_PM') == '1':
    (home / 'auth.json').write_text('migration changed auth')
    sys.exit(7)
source = pathlib.Path.cwd()
state = home / 'installs' / hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
state.mkdir(parents=True, exist_ok=True)
extras = [sys.argv[i+1] for i, arg in enumerate(sys.argv) if arg == '--extra']
(state / 'facts.json').write_text(json.dumps({'packages': {'venv': {'extras': extras}}}))
launcher = source / '.hermes/bin/hermes'
launcher.parent.mkdir(parents=True, exist_ok=True)
launcher.write_text('#!/bin/sh\\nexit 0\\n')
launcher.chmod(0o700)
''')
        subprocess.run(['git', '-C', str(self.origin), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.origin), 'commit', '-qm', 'modern PM'], check=True)
        subprocess.run(['git', '-C', str(self.origin), 'branch', '-M', 'main'], check=True)
        subprocess.run(['git', '-C', str(self.code), 'remote', 'add', 'origin', str(self.origin)], check=True)
        bootstrap = executable(self.home / 'bin/python3.14', '''#!/bin/bash
# Offline supported-bootstrap stand-in: execute the real PM fixture subprocess.
if [ "$1" = -c ]; then exit 0; fi
exec /usr/bin/python3 "$@"
''')
        self.env['HERMES_BOOTSTRAP_PYTHON'] = str(bootstrap)
        self.launcher.write_text('''#!/bin/bash
case "$1" in
--version) [ -f "$AGENT_DIR/pm/lock.json" ] || exit 92; echo fixture-modern;;
update) echo 'old updater MUST NOT run' >&2; exit 93;;
webapp) [ -f "$AGENT_DIR/pm/lock.json" ] || { echo 'old webapp MUST NOT run' >&2; exit 94; };;
*) exit 95;;
esac
''')
        (self.home / '.bootstrap-lock').touch()

    def test_legacy_ensure_does_not_poison_future_snapshot_with_modern_recipe(self):
        self.legacy()
        with self.assertRaisesRegex(ValueError, 'pre-PM.*safe-update'):
            addon_runtime.ensure_runtime(self.home, self.code, '/must-not-run')
        self.assertFalse((self.home / 'pm-reconstruction.json').exists())
        snap = self.snapshot()
        self.assertFalse((snap / 'pm-reconstruction.json').exists())

    def test_manual_upgrade_uses_snapshot_then_supported_bootstrap_and_releases_boot_lock(self):
        self.legacy()
        with server() as svc:
            result = self.run_update('--now', WEBAPP_PORT=str(svc.server_port))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.code / 'pm/lock.json').exists())
        self.assertFalse((self.home / '.bootstrap-lock').exists())
        self.assertFalse((self.home / '.update-pending').exists())
        self.assertFalse((self.home / '.update-running').exists())
        self.assertEqual((self.home / 'auth.json').read_text(), self.saved['auth.json'])
        snapshots = list((self.home / 'backups/pre-update').iterdir())
        self.assertEqual(len(snapshots), 1)
        self.assertEqual((snapshots[0] / '.code-rev').read_text().strip(), self.rev)
        self.assertNotIn('old updater MUST NOT run', (self.home / 'logs/safe-update.log').read_text())

    def test_boot_auto_update_migrates_before_runtime_and_failure_keeps_recovery_retryable(self):
        for fail in ('1', '0'):
            with self.subTest(fail=fail):
                # First failed boot restores old code; second boot retries successfully.
                if fail == '1':
                    self.legacy()
                binaries = self.home / 'addon-bin'
                binaries.mkdir(exist_ok=True)
                for name in ('hermes-safe-update.sh', 'addon_runtime.py'):
                    shutil.copy2(runtime.ADDON / name, binaries / name)
                script = (runtime.ADDON / 'run.sh').read_text()
                section = script[script.index('# Keep all existing /data'):script.index('HERMES_CONFIG=')]
                harness = '''function bashio::var.true() { [ "$1" = true ]; }
function bashio::log.info() { printf '%s\\n' "$*"; }
function bashio::log.warning() { printf '%s\\n' "$*"; }
AGENT_DST="$HERMES_HOME/agent-code"
AGENT_SRC=/unavailable-seed
ADDON_BIN="$HERMES_HOME/addon-bin"
SAFE_UPDATE="$ADDON_BIN/hermes-safe-update.sh"
BOOTSTRAP_LOCK="$HERMES_HOME/.bootstrap-lock"
AUTO_UPDATE_AGENT=true
''' + section + '\nprintf "RUNTIME_OK=%s\\n" "$RUNTIME_OK"\n'
                result = subprocess.run(['bash', '-c', harness], env={**self.env, 'FAIL_PM': fail}, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('RUNTIME_OK=' + ('false' if fail == '1' else 'true'), result.stdout)
                self.assertEqual((self.home / '.bootstrap-lock').exists(), fail == '1')
                self.assertFalse((self.home / '.update-running').exists())
                self.assertEqual((self.code / 'pm/lock.json').exists(), fail == '0')

    def test_failed_upgrade_restores_legacy_code_data_and_can_retry_without_pm_lock_trap(self):
        self.legacy()
        # Image stripping / local tracked edits must survive rollback too.
        (self.code / 'hermes').write_text('# local edit retained\n')
        # Reproduce the poisonous default recipe left by the former failed boot.
        (self.home / 'pm-reconstruction.json').write_text('{"schema":1,"extras":["all","messaging"],"declined_extras":[],"tool_versions":{},"source_input_sha256":{}}')
        result = self.run_update('--boot', FAIL_PM='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('rollback restored', result.stderr)
        self.assertFalse((self.code / 'pm/lock.json').exists())
        self.assertFalse((self.home / 'pm-reconstruction.json').exists())
        self.assertEqual((self.code / 'hermes').read_text(), '# local edit retained\n')
        self.assertEqual((self.home / 'auth.json').read_text(), self.saved['auth.json'])
        self.assertFalse((self.home / '.update-running').exists())
        self.assertFalse((self.home / '.update-pending').exists())
        self.assertTrue((self.home / '.bootstrap-lock').exists(), 'old core must stay behind recovery, not receive modern gateway flags')
        # Kernel lock is released; a retry upgrades rather than demanding a nonexistent PM lock.
        result = self.run_update('--boot')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / '.bootstrap-lock').exists())
        self.assertTrue((self.home / '.update-pending').exists())

    def test_build_failure_after_pm_install_restores_old_launcher_and_personal_state(self):
        self.legacy()
        self.launcher.write_text(self.launcher.read_text().replace('webapp) ', 'webapp) exit 88; '))
        result = self.run_update('--boot')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Webapp build failed', result.stderr)
        self.assertIn('rollback restored', result.stderr)
        self.assertFalse((self.code / '.hermes').exists())
        self.assertFalse((self.code / 'pm/lock.json').exists())
        self.assertFalse((self.home / 'pm-reconstruction.json').exists())
        self.assertFalse((self.home / '.update-running').exists())
        for name, value in self.saved.items():
            if name != 'agent-code/.hermes/bin/hermes':
                self.assertEqual((self.home / name).read_text(), value, name)

    def test_failed_snapshot_never_fetches_or_changes_legacy_source(self):
        self.legacy()
        (self.home / 'config.yaml').unlink()
        result = self.run_update('--boot')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('snapshot failed', result.stderr)
        self.assertNotIn('migration: fetching', (self.home / 'logs/safe-update.log').read_text())
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.code), 'rev-parse', 'HEAD'], text=True).strip(), self.rev)
        self.assertFalse((self.home / '.update-running').exists())


if __name__ == '__main__':
    unittest.main()
