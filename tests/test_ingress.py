"""Real alternate nginx + ttyd + front-door regression, no live config touched."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from test_runtime import ADDON, executable, server


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@unittest.skipUnless(Path('/usr/sbin/nginx').exists() and shutil.which('ttyd'), 'needs nginx and ttyd')
class IngressRig(unittest.TestCase):
    def test_gateway_helper_down_recovery_logs_terminal_remain_available(self):
        with tempfile.TemporaryDirectory(prefix='hermes-ingress-', dir=os.environ.get('TMPDIR')) as tmp:
            home = Path(tmp)
            logs = home / 'logs'
            logs.mkdir()
            (logs / 'gateway-addon.log').write_text('isolated gateway failure fixture\n')
            (home / '.env').write_text('not exposed')
            http, ticket, terminal, app = port(), port(), port(), port()
            config = (ADDON / 'nginx.conf').read_text()
            for old, new in [('0.0.0.0:8787', '127.0.0.1:' + str(http)),
                             ('127.0.0.1:9130', '127.0.0.1:' + str(ticket)),
                             ('127.0.0.1:7681', '127.0.0.1:' + str(terminal)),
                             ('127.0.0.1:9120', '127.0.0.1:' + str(app)),
                             ('/run/nginx.pid', str(home / 'nginx.pid')),
                             ('/dev/stderr', str(home / 'error.log')),
                             ('/dev/stdout', str(home / 'access.log')),
                             ('/data/hermes/logs', str(logs))]:
                config = config.replace(old, new)
            path = home / 'nginx.conf'
            path.write_text(config)
            nginx = subprocess.Popen(['/usr/sbin/nginx', '-c', str(path), '-p', str(home)])
            ttyd = subprocess.Popen([shutil.which('ttyd'), '--port', str(terminal), '--interface', '127.0.0.1',
                                     '--base-path', '/terminal', '--writable', '/bin/bash'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            def request(path, method='GET'):
                req = urllib.request.Request('http://127.0.0.1:%s%s' % (http, path), method=method,
                                             headers={'X-Ingress-Path': '/api/hassio_ingress/fixture'})
                try:
                    response = opener.open(req, timeout=10)
                except urllib.error.HTTPError as error:
                    response = error
                return response.code, response.headers, response.read()
            try:
                for _ in range(50):
                    try:
                        if request('/recovery/')[0] == 200 and request('/terminal/')[0] == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(.1)
                code, _, body = request('/')
                self.assertEqual(code, 200)
                self.assertIn(b'hermes-safe-update.sh --rollback', body)
                self.assertIn(b'/api/hassio_ingress/fixture/terminal/', body)
                self.assertEqual(request('/ready')[0], 502)
                self.assertEqual(request('/health')[0], 502)
                self.assertEqual(request('/webapp/')[0], 200, 'dead Webapp must land on independent recovery')
                code, headers, _ = request('/old/')
                self.assertEqual(code, 302)
                self.assertEqual(headers['Location'], '/api/hassio_ingress/fixture/recovery/')
                self.assertEqual(request('/logs/gateway-addon.log')[2], b'isolated gateway failure fixture\n')
                self.assertEqual(request('/logs/.env')[0], 404)
                self.assertEqual(request('/logs/../.env')[0], 302)
                code, _, body = request('/terminal/')
                self.assertEqual(code, 200)
                self.assertTrue(b'ttyd - Terminal' in body)
                self.assertFalse(b'src="/terminal/' in body)
            finally:
                for proc in (nginx, ttyd):
                    proc.terminate()
                    proc.wait(timeout=10)

    def test_front_door_failure_head_and_ticket_success(self):
        with tempfile.TemporaryDirectory(prefix='hermes-ticket-', dir=os.environ.get('TMPDIR')) as tmp:
            home = Path(tmp)
            ticket_port = port()
            token = 'A' * 43
            launcher = executable(home / 'hermes', '#!/bin/sh\necho "http://127.0.0.1/webapp/launch#ticket=' + token + '"\n')
            with server('ready') as svc:
                env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HERMES_HOME': str(home),
                       'WEBAPP_PORT': str(svc.server_port), 'TICKET_PORT': str(ticket_port), 'HERMES_BIN': str(launcher)}
                proc = subprocess.Popen(['/usr/bin/python3', str(ADDON / 'webapp-ticket.py')], env=env)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                def request(method='GET', prefix='/api/hassio_ingress/test'):
                    req = urllib.request.Request('http://127.0.0.1:%s/' % ticket_port,
                        method=method, headers={'X-Ingress-Path': prefix})
                    try:
                        response = opener.open(req, timeout=10)
                    except urllib.error.HTTPError as error:
                        response = error
                    return response.code, response.headers, response.read()
                try:
                    for _ in range(50):
                        try:
                            result = request()
                            break
                        except OSError:
                            time.sleep(.1)
                    code, headers, body = result
                    self.assertEqual(code, 302)
                    self.assertEqual(headers['Location'], '/api/hassio_ingress/test/webapp/webapp/launch#ticket=' + token)
                    self.assertEqual(headers['Cache-Control'], 'no-store')
                    self.assertEqual(body, b'')
                    self.assertEqual(request('HEAD')[2], b'')
                    self.assertEqual(request(prefix='//evil.example')[0], 400)
                    executable(launcher, '#!/bin/sh\nexit 1\n')
                    code, _, body = request()
                    self.assertEqual(code, 200)
                    self.assertIn(b'/recovery/', body)
                    self.assertNotIn(b'/old/', body)
                finally:
                    proc.terminate()
                    proc.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
