"""Run the actual boot entrypoint in an isolated home and sanitized boot PATH."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from test_ingress import port
from test_runtime import ADDON, executable


@unittest.skipUnless(Path('/usr/sbin/nginx').exists() and shutil.which('ttyd'), 'needs nginx and ttyd')
class BootRig(unittest.TestCase):
    def test_exact_boot_path_fresh_mirror_build_and_optional_failures_keep_recovery(self):
        with tempfile.TemporaryDirectory(prefix='hermes-boot-', dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            home, seed, binaries = root / 'home', root / 'seed', root / 'bin'
            home.mkdir()
            binaries.mkdir()
            executable(seed / 'venv/bin/tool', '#!' + str(seed) + '/venv/bin/python\nfixture\n')
            (seed / 'hermes').write_text('fixture source')
            executable(seed / 'venv/bin/python', '#!/bin/sh\nexit 0\n')
            for name in ['hermes-safe-update.sh', 'addon_config.py', 'addon_runtime.py', 'addon_health.py', 'webapp-ticket.py']:
                shutil.copy2(ADDON / name, binaries / name)
                (binaries / name).chmod(0o700)
            (binaries / 'ttyd').symlink_to(shutil.which('ttyd'))
            executable(binaries / 'bw-setup.sh', '#!/bin/sh\necho optional-fixture-failed >&2\nexit 42\n')
            launcher = executable(binaries / 'hermes-fixture', '#!/bin/sh\necho build-or-gateway-fixture-failed >&2\nexit 7\n')
            # Keep old chats and credentials in this isolated home across bootstrap.
            (home / 'webui/sessions').mkdir(parents=True)
            (home / 'webui/sessions/old.json').write_text('old chat retained')
            (home / '.env').write_text('USER_API_KEY=fixture-kept\nCLAUDE_CODE_OAUTH_TOKEN=fixture-kept\n')
            http, ticket, terminal, app = port(), port(), port(), port()
            config = (ADDON / 'nginx.conf').read_text()
            for before, after in [('0.0.0.0:8787', '127.0.0.1:' + str(http)),
                                  ('127.0.0.1:9130', '127.0.0.1:' + str(ticket)),
                                  ('127.0.0.1:7681', '127.0.0.1:' + str(terminal)),
                                  ('127.0.0.1:9120', '127.0.0.1:' + str(app)),
                                  ('/run/nginx.pid', str(root / 'nginx.pid')),
                                  ('/dev/stderr', str(root / 'nginx-errors.log')),
                                  ('/dev/stdout', str(root / 'nginx-access.log')),
                                  ('/data/hermes/logs', str(home / 'logs'))]:
                config = config.replace(before, after)
            nginx_config = root / 'nginx.conf'
            nginx_config.write_text(config)
            # Only the terminal test port changes; production startup logic is unmodified.
            run = root / 'run.sh'
            run.write_text((ADDON / 'run.sh').read_text().replace('--port 7681', '--port ' + str(terminal)))
            harness = root / 'boot.sh'
            harness.write_text('''#!/bin/bash
function bashio::config() {
 case "$1" in timezone) printf UTC;; enable_terminal|auto_configure_mcp) printf true;; auto_update_agent) printf false;; *) printf '';; esac
}
function bashio::var.true() { [ "$1" = true ]; }
function bashio::log.info() { printf '%s\\n' "$*"; }
function bashio::log.warning() { printf '%s\\n' "$*"; }
source "$BOOT_SCRIPT"
''')
            output = (root / 'boot.log').open('w')
            # Equivalent to env -i: no live tokens, PM activation, config, or Python paths.
            env = {'HERMES_HOME': str(home), 'PATH': '/command:/usr/local/bin:/usr/bin:/bin',
                   'AGENT_SRC': str(seed), 'ADDON_BIN': str(binaries), 'HERMES_BIN': str(launcher),
                   'NGINX_CONFIG': str(nginx_config), 'BOOT_SCRIPT': str(run),
                   'CLAUDE_CREDENTIALS_FILE': str(root / 'no-credentials.json'),
                   'TICKET_PORT': str(ticket), 'WEBAPP_PORT': str(app), 'STARTUP_GRACE_SECONDS': '2'}
            process = subprocess.Popen(['/usr/bin/env', '-i', *[key + '=' + value for key, value in env.items()],
                                        '/bin/bash', str(harness)], stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def request(path):
                try:
                    response = opener.open('http://127.0.0.1:%s%s' % (http, path), timeout=10)
                except urllib.error.HTTPError as error:
                    response = error
                return response.code, response.read()
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if (home / '.webapp-build-failed').exists():
                        break
                    self.assertIsNone(process.poll(), 'boot exited: ' + (root / 'boot.log').read_text())
                    time.sleep(.1)
                self.assertTrue((home / '.webapp-build-failed').exists())
                self.assertIsNone(process.poll())
                self.assertEqual((home / 'webui/sessions/old.json').read_text(), 'old chat retained')
                self.assertIn('CLAUDE_CODE_OAUTH_TOKEN=fixture-kept', (home / '.env').read_text())
                self.assertIn('USER_API_KEY=fixture-kept', (home / '.env').read_text())
                self.assertIn(str(home / 'agent-code/venv/bin/python'), (home / 'agent-code/venv/bin/tool').read_text())
                self.assertEqual(request('/recovery/')[0], 200)
                self.assertEqual(request('/terminal/')[0], 200)
                self.assertEqual(request('/ready')[0], 503)
                # Grace expires despite failed build and respawn loop; no false permanent 200.
                time.sleep(2.1)
                self.assertEqual(request('/health')[0], 503)
                self.assertIn('Optional Bitwarden setup unavailable', (root / 'boot.log').read_text())
            finally:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                output.close()


if __name__ == '__main__':
    unittest.main()
