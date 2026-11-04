"""Isolated add-on reliability regressions; no live data or services used."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / 'hermes_assistant'


def load(name):
    spec = importlib.util.spec_from_file_location(name, ADDON / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BootContractTests(unittest.TestCase):
    def test_no_classic_runtime_and_both_build_paths_ship_helpers(self):
        run = (ADDON / 'run.sh').read_text()
        for legacy in ['WEBUI_', '/opt/hermes-webui', 'auto_update_webui', 'server.py']:
            self.assertNotIn(legacy, run)
        for name in ['addon_health.py', 'addon_config.py', 'addon_runtime.py', 'webapp-ticket.py', 'hermes-safe-update.sh']:
            self.assertIn('COPY ' + name + ' /usr/local/bin/', (ADDON / 'Dockerfile').read_text())
            self.assertIn('COPY ' + name + ' /usr/local/bin/', (ROOT / '.github/workflows/retag.yml').read_text())
            self.assertIn('hermes_assistant/' + name, (ROOT / '.github/workflows/retag.yml').read_text())
        self.assertIn('python3-yaml', (ADDON / 'Dockerfile').read_text())
        self.assertIn('python3-yaml', (ROOT / '.github/workflows/retag.yml').read_text())
        self.assertIn('COPY hermes-launcher.sh /usr/local/bin/hermes', (ADDON / 'Dockerfile').read_text())
        self.assertIn('hermes_assistant/hermes-launcher.sh', (ROOT / '.github/workflows/retag.yml').read_text())
        self.assertNotIn('patch_old_python', (ADDON / 'hermes-safe-update.sh').read_text())
        self.assertIn('/usr/bin/python3 "${ADDON_BIN}/addon_config.py"', run)
        self.assertNotIn('8788', (ADDON / 'nginx.conf').read_text())


class UpdateTests(unittest.TestCase):
    def test_strict_health_rejects_an_open_gateway_without_webapp(self):
        script = (ADDON / 'hermes-safe-update.sh').read_text()
        self.assertIn('addon_health.py', script)
        self.assertIn('--ready', script)
        self.assertNotIn('port check only', script)


class ReadinessTests(unittest.TestCase):
    def test_gateway_http_without_bundle_is_not_ready(self):
        path = ADDON / 'addon_health.py'
        self.assertTrue(path.exists(), 'need independent gateway/Webapp readiness, not classic WebUI health')
        health = load('addon_health')
        with tempfile.TemporaryDirectory() as home:
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = b'{"ok":true}' if self.path == '/api/health' else b'{"error":"not built"}'
                    self.send_response(200 if self.path == '/api/health' else 503)
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                state = health.readiness(home, server.server_port)
                self.assertFalse(state['ready'])
                self.assertTrue(state['gateway'])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
