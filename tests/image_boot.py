#!/usr/bin/env python3
"""Run INSIDE a disposable CI image, never against a live add-on.

Exercises the real fresh seed -> /data mirror -> Webapp build -> gateway ->
HTTP bundle -> ticket path with the exact startup PATH and no credentials.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

assert os.environ.get('HERMES_IMAGE_SMOKE') == '1', 'disposable image smoke only'
assert not Path('/data/hermes/config.yaml').exists(), 'requires an empty image data volume'
assert not Path('/opt/hermes-webui').exists(), 'classic payload must not ship'
BOOT = '''
function bashio::config() {
  case "$1" in
    timezone) printf 'UTC';;
    enable_terminal|auto_configure_mcp) printf 'true';;
    auto_update_agent) printf 'false';;
    *) printf '';;
  esac
}
function bashio::var.true() { [ "$1" = true ]; }
function bashio::log.info() { printf '%s\\n' "$*"; }
function bashio::log.warning() { printf '%s\\n' "$*"; }
source /run.sh
'''
# No inherited tokens, Python paths, PM activation, or host environment.
env = {'HERMES_HOME': '/data/hermes', 'PATH': '/command:/usr/local/bin:/usr/bin:/bin',
       'HOME': '/root', 'TMPDIR': '/data/smoke-tmp'}
Path(env['TMPDIR']).mkdir(parents=True, exist_ok=True)
# Mandatory behavioral capability gate, not a skip based on the version string.
from git_update_fixture import PartialClone
assert subprocess.check_output(['git', '--version'], env=env, text=True).strip() == 'git version 2.55.0'
assert Path('/usr/local/bin/git').resolve() == Path('/opt/git/bin/git')
assert Path('/opt/git/libexec/git-core/git-remote-https').is_file()
with tempfile.TemporaryDirectory(dir=env['TMPDIR']) as git_root:
    PartialClone(git_root).passive_probes()
print('PASS: image Git 2.55.0, real blobless clone, missing-target probes do not lazy-fetch')
log = Path('/data/image-boot.log').open('w')
process = subprocess.Popen(['/bin/bash', '-c', BOOT], env=env, stdout=log, stderr=subprocess.STDOUT,
                           start_new_session=True)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    deadline = time.monotonic() + 900
    ready = False
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('add-on wrapper exited before readiness')
        try:
            with opener.open('http://127.0.0.1:8787/ready', timeout=10) as response:
                ready = json.load(response).get('ready') is True
        except (OSError, ValueError):
            pass
        if ready:
            break
        time.sleep(3)
    assert ready, 'fresh gateway/Webapp failed strict readiness'
    # CLI help can pass while mirrored imports are wrong; verify live source and listener.
    source = subprocess.check_output(['/usr/local/bin/hermes', '--version'], env=env, text=True)
    assert '/data/hermes/agent-code' in source, 'launcher did not switch to mirrored source'
    subprocess.run(['/usr/local/bin/hermes', 'webapp', '--build-only'], env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # A one-use launch ticket must actually be minted by the running gateway.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        direct.open('http://127.0.0.1:8787/', timeout=45)
        raise AssertionError('front door did not redirect')
    except urllib.error.HTTPError as response:
        assert response.code == 302 and '#ticket=' in response.headers['Location'], 'no working launch ticket'
    for path in ('/recovery/', '/terminal/'):
        with opener.open('http://127.0.0.1:8787' + path, timeout=10) as response:
            assert response.status == 200
    assert not Path('/data/hermes/.webapp-build-failed').exists()
    # A separate credential-free process exercises the exact Settings HTTP route
    # with offline release inputs and the image's real mirrored source/Git/guard.
    subprocess.run(['/opt/hermes-agent-code/venv/bin/python', '/tests/source_update_smoke.py'],
                   env={**env, 'PYTHONPATH': '/data/hermes/agent-code'}, check=True, timeout=120)
    print('PASS: fresh image mirror, config, real Webapp build, gateway HTTP bundle, ticket, terminal, recovery')
except Exception:
    # This fixture has no credentials; logs are safe in CI diagnostics.
    for path in [Path('/data/image-boot.log'), *Path('/data/hermes/logs').glob('*.log')]:
        print('\n--- ' + path.name + ' ---\n' + path.read_text(errors='replace')[-8000:])
    raise
finally:
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    log.close()
