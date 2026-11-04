"""PM intent schema/export/restore regressions, without network or live state."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_runtime import ADDON
import test_runtime as runtime
import addon_runtime


class RuntimeRecipe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.source = self.home / 'agent-code'
        self.source.mkdir()
        (self.source / 'pm').mkdir()
        (self.source / 'pm/lock.json').write_text(json.dumps({'packages': {'node': {'version': '22.1'}}}))
        (self.source / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
        self.state = addon_runtime.selection(self.home, self.source)
        self.state.mkdir(parents=True)
        self.environment = self.home / 'environments/old'
        (self.environment / 'bin').mkdir(parents=True)
        (self.environment / 'bin/python').touch()
        (self.state / 'facts.json').write_text(json.dumps({'packages': {'venv': {'extras': ['web', 'messaging'], 'environment': str(self.environment)}}}))
        (self.state / 'declined-extras.json').write_text('{"declined":["speech"]}')
        (self.home / 'tools').mkdir()
        (self.home / 'tools/facts.json').write_text('{"packages":{"node":{"version":"22.1","entry":"node-fixture"}}}')
        self.recipe = self.home / 'pm-reconstruction.json'
        addon_runtime.export_runtime_recipe(self.home, self.source, self.recipe)

    def tearDown(self):
        self.tmp.cleanup()

    def test_recipe_has_only_portable_intent_and_input_hashes(self):
        data = json.loads(self.recipe.read_text())
        self.assertEqual(data['extras'], ['messaging', 'web'])
        self.assertEqual(data['declined_extras'], ['speech'])
        self.assertEqual(data['tool_versions'], {'node': '22.1'})
        self.assertNotIn(str(self.home), self.recipe.read_text())
        self.assertNotIn('environment', self.recipe.read_text())
        self.assertEqual(self.recipe.stat().st_mode & 0o777, 0o600)
        self.assertEqual(data['source_input_sha256']['pyproject.toml'], hashlib.sha256((self.source / 'pyproject.toml').read_bytes()).hexdigest())

    def test_rebuild_uses_modern_bootstrap_and_explicit_extras_and_rekeys_declines(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)
        with patch.object(addon_runtime.subprocess, 'run', side_effect=run):
            addon_runtime.ensure_runtime(self.home, self.source, '/supported/python3.14')
        self.assertEqual([item[0][0] for item in calls], ['/supported/python3.14'] * 3)
        self.assertEqual(calls[1][0][1:], ['-m', 'pm.cli', 'install', 'node'])
        self.assertEqual(calls[2][0][1:], ['-m', 'pm.cli', 'install', '--extra', 'messaging', '--extra', 'web'])
        self.assertEqual(calls[2][1]['env']['HERMES_RUNTIME_DIR'], str(self.home / 'tools'))
        self.assertEqual(json.loads((self.state / 'declined-extras.json').read_text())['declined'], ['speech'])

    def test_source_mismatch_fails_before_network_or_environment_changes(self):
        (self.source / 'pyproject.toml').write_text('wrong revision')
        with patch.object(addon_runtime.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'restored source'):
                addon_runtime.ensure_runtime(self.home, self.source, '/supported/python3.14')
            command.assert_not_called()

    def test_old_bootstrap_is_rejected_not_tar_filter_patched(self):
        with patch.object(addon_runtime.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'old-python')):
            with self.assertRaises(subprocess.CalledProcessError):
                addon_runtime.ensure_runtime(self.home, self.source, '/old/python3.11')
        self.assertNotIn('tf.extractall', (ADDON / 'hermes-safe-update.sh').read_text())

    def test_metacharacters_and_source_traversal_are_rejected(self):
        data = json.loads(self.recipe.read_text())
        data['extras'] = ['web; touch /bad']
        with self.assertRaises(ValueError):
            addon_runtime.validate(data)
        data['extras'] = ['web']
        data['source_input_sha256'] = {'../auth.json': 'a' * 64}
        with self.assertRaises(ValueError):
            addon_runtime.validate(data)

    def test_existing_modern_selection_does_not_rebuild(self):
        (self.source / '.hermes/bin').mkdir(parents=True)
        (self.source / '.hermes/bin/hermes').touch()
        (self.home / 'tools/node-fixture').mkdir()
        with patch.object(addon_runtime.subprocess, 'run') as command:
            addon_runtime.ensure_runtime(self.home, self.source, '/supported/python3.14')
            command.assert_not_called()

    def test_later_user_extras_and_declines_are_not_pinned_back_on_reboot(self):
        (self.source / '.hermes/bin').mkdir(parents=True)
        (self.source / '.hermes/bin/hermes').touch()
        (self.home / 'tools/node-fixture').mkdir()
        data = json.loads((self.state / 'facts.json').read_text())
        data['packages']['venv']['extras'].append('speech')
        (self.state / 'facts.json').write_text(json.dumps(data))
        (self.state / 'declined-extras.json').write_text('{"declined":[]}')
        with patch.object(addon_runtime.subprocess, 'run') as command:
            addon_runtime.ensure_runtime(self.home, self.source, '/must-not-be-used/python')
            command.assert_not_called()
        self.assertIn('speech', json.loads(self.recipe.read_text())['extras'])
        self.assertEqual(json.loads(self.recipe.read_text())['declined_extras'], [])


class SnapshotPM(unittest.TestCase):
    home: Path
    code: Path

    setUp = runtime.UpdateRig.setUp
    tearDown = runtime.UpdateRig.tearDown
    run_update = runtime.UpdateRig.run_update
    snapshot = runtime.UpdateRig.snapshot

    def test_rollback_restores_pm_intent_selection_and_declines(self):
        state = addon_runtime.selection(self.home, self.code)
        state.mkdir(parents=True)
        environment = self.home / 'environments/old'
        (environment / 'bin').mkdir(parents=True)
        (environment / 'bin/python').touch()
        (self.code / 'pm').mkdir(exist_ok=True)
        (self.code / 'pm/lock.json').write_text('{"packages":{},"fixture":"restored"}')
        # Include matching source inputs in the git revision restored by rollback.
        subprocess.run(['git', '-C', str(self.code), 'add', 'pm'], check=True)
        subprocess.run(['git', '-C', str(self.code), 'commit', '-qm', 'PM fixture'], check=True)
        (state / 'facts.json').write_text(json.dumps({'packages': {'venv': {'extras': ['web'], 'environment': str(environment)}}}))
        (state / 'declined-extras.json').write_text('{"declined":["speech"]}')
        snap = self.snapshot()
        (state / 'facts.json').write_text('{}')
        (state / 'declined-extras.json').write_text('{"declined":[]}')
        result = self.run_update('--rollback', str(snap))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.home / 'pm-reconstruction.json').read_text())['extras'], ['web'])
        self.assertEqual(json.loads((state / 'declined-extras.json').read_text())['declined'], ['speech'])
        self.assertFalse((self.home / '.update-running').exists())

    def test_snapshot_exports_pm_intent_without_binaries(self):
        state = addon_runtime.selection(self.home, self.code)
        state.mkdir(parents=True)
        (state / 'facts.json').write_text(json.dumps({'packages': {'venv': {'extras': ['web'], 'environment': str(self.home / 'environments/private')}}}))
        (state / 'declined-extras.json').write_text('{"declined":["speech"]}')
        snap = self.snapshot()
        recipe = json.loads((snap / 'pm-reconstruction.json').read_text())
        self.assertEqual(recipe['extras'], ['web'])
        self.assertEqual(recipe['declined_extras'], ['speech'])
        self.assertNotIn(str(self.home), json.dumps(recipe))
        self.assertIn('pm-reconstruction.json', (snap / '.include').read_text().splitlines())
        self.assertFalse((snap / 'installs/fixture/environments').exists())


if __name__ == '__main__':
    unittest.main()
