"""Executable integration tests use only temporary homes and loopback servers."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / 'hermes_assistant'
sys.path.insert(0, str(ADDON))
import addon_health
import addon_config


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o700)
    return path


@contextmanager
def server(mode='ready'):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, kind, body = 200, 'application/json', b'{"ok":true}'
            if self.path == '/':
                kind = 'text/html'
                body = b'<html><script>window.__HERMES_UI_SURFACE__="webapp";</script><script type="module" src="./assets/index-test.js"></script></html>'
                if mode == 'missing':
                    status = 503
                if mode == 'login':
                    body = b'<html>Sign in</html>'
            elif self.path.startswith('/assets/'):
                kind, body = 'application/javascript', b'console.log("fixture");'
                if mode == 'asset-missing':
                    status = 404
            elif self.path == '/api/status':
                body = b'{"gateway_running":true,"gateway_state":"running"}'
                if mode == 'starting':
                    body = b'{"gateway_running":false,"gateway_state":"starting"}'
            elif mode == 'malformed':
                body = b'[]'
            elif mode == 'gateway-bad':
                body = b'{"ok":false}'
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"ok"}}]}')
        def log_message(self, format, *args):
            pass
    service = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=service.serve_forever, daemon=True)
    worker.start()
    try:
        yield service
    finally:
        service.shutdown()
        service.server_close()
        worker.join(timeout=3)


class HealthStates(unittest.TestCase):
    def test_strict_readiness_requires_gateway_and_bundle_and_real_js(self):
        with tempfile.TemporaryDirectory() as home:
            for mode in ('ready', 'missing', 'login', 'asset-missing', 'gateway-bad', 'starting', 'malformed'):
                with self.subTest(mode=mode), server(mode) as svc:
                    state = addon_health.readiness(home, svc.server_port)
                    self.assertEqual(state['ready'], mode == 'ready')

    def test_grace_is_bounded_and_not_reopened_after_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            started = home / '.addon-started'
            started.touch()
            os.utime(started, (100, 100))
            down = {'ready': False}
            self.assertTrue(addon_health.watchdog(down, home, now=109, grace=10))
            self.assertFalse(addon_health.watchdog(down, home, now=110, grace=10))
            self.assertTrue(addon_health.watchdog({'ready': True}, home, now=109, grace=10))
            self.assertFalse(addon_health.watchdog(down, home, now=109, grace=10))
            update = home / '.update-pending'
            update.touch()
            os.utime(update, (200, 200))
            self.assertTrue(addon_health.watchdog(down, home, now=209, grace=10))
            self.assertFalse(addon_health.watchdog(down, home, now=210, grace=10))

    def test_stale_build_flag_never_extends_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / '.webapp-building').touch()
            state = addon_health.readiness(home, 1)
            self.assertEqual(state['state'], 'building')
            self.assertFalse(addon_health.watchdog(state, home, now=time.time(), grace=1))


class TicketFailures(unittest.TestCase):
    def test_timeout_and_nonzero_release_ticket_lock(self):
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('ticket_test', ADDON / 'webapp-ticket.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 30)):
            self.assertIsNone(module.mint_ticket())
        self.assertFalse(module._mint_lock.locked())
        with patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess('fixture', 7, '', '')):
            self.assertIsNone(module.mint_ticket())
        self.assertFalse(module._mint_lock.locked())


class ConfigTests(unittest.TestCase):
    def test_atomic_config_preserves_provider_auth_plugins_and_other_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.yaml')
            path.write_text('model: {provider: copilot}\nplugins: {enabled: [github-copilot]}\nmcp_servers: {other: {url: http://example.invalid}}\n')
            addon_config.configure(path, True, 'http://fixture/', 'test-token')
            cfg = addon_config.yaml.safe_load(path.read_text())
            self.assertEqual(cfg['model']['provider'], 'copilot')
            self.assertEqual(cfg['plugins']['enabled'], ['github-copilot'])
            self.assertIn('other', cfg['mcp_servers'])
            self.assertEqual(cfg['webapp'], {'enabled': True, 'host': '127.0.0.1', 'port': 9120})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_bad_optional_mcp_does_not_prevent_webapp_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.yaml')
            path.write_text('mcp_servers: invalid-shape\nmodel: {provider: fixture}\n')
            addon_config.configure(path, True, 'http://fixture', 'test-token')
            cfg = addon_config.yaml.safe_load(path.read_text())
            self.assertTrue(cfg['webapp']['enabled'])
            self.assertEqual(cfg['mcp_servers'], 'invalid-shape')

    def test_invalid_config_is_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.yaml')
            original = b'model: [broken\n'
            path.write_bytes(original)
            with self.assertRaises(addon_config.yaml.YAMLError):
                addon_config.configure(path, True, 'http://fixture', 'test-token')
            self.assertEqual(path.read_bytes(), original)


class UpdateRig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hermes-update-', dir=os.environ.get('TMPDIR'))
        self.home = Path(self.tmp.name)
        self.code = self.home / 'agent-code'
        self.code.mkdir()
        for command in (['git', 'init', '-q', str(self.code)],
                        ['git', '-C', str(self.code), 'config', 'user.email', 'test@example.invalid'],
                        ['git', '-C', str(self.code), 'config', 'user.name', 'Test']):
            subprocess.run(command, check=True, capture_output=True)
        (self.code / 'version').write_text('before')
        subprocess.run(['git', '-C', str(self.code), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.code), 'commit', '-qm', 'fixture'], check=True)
        self.rev = subprocess.check_output(['git', '-C', str(self.code), 'rev-parse', 'HEAD'], text=True).strip()
        self.saved = {}
        for name in ['config.yaml', '.env', 'auth.json', 'memories/m.md', 'wiki/index.md', 'skills/s/SKILL.md',
                     'scripts/job.py', 'cron/jobs.json', 'plugins/github-copilot/plugin.yaml',
                     'desktop-plugins/test/plugin.json', 'webapp/session-token', 'vault/test.json',
                     'ui-preferences.json', 'webui/sessions/old.json', 'sessions/history.jsonl',
                     'installs/fixture/facts.json', 'installs/fixture/inputs/selection.json',
                     'agent-code/.hermes/bin/hermes', 'custom-extra/note', 'optional-extra/item',
                     'kanban/boards/board.json', 'mcp-tokens/service.json', 'native-sign-in-tokens.json', 'sign-in-registration.json']:
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            content = 'model: {provider: fixture}\n' if name == 'config.yaml' else 'original ' + name
            path.write_text(content)
            self.saved[name] = content
        extra = self.home / 'skills/devops/backups/config.json'
        extra.parent.mkdir(parents=True)
        extra.write_text(json.dumps({'hermes_include': ['custom-extra'], 'hermes_optional_include': ['optional-extra', 'missing-optional']}))
        # Binaries are deliberately not included in the snapshot.
        executable(self.home / 'installs/fixture/environments/large-binary', '#!/bin/sh\nexit 0\n')
        self.db = sqlite3.connect(self.home / 'state.db')
        self.db.execute('pragma journal_mode=WAL')
        self.db.execute('create table messages (id integer primary key, text text)')
        self.db.execute('insert into messages(text) values (?)', ('WAL-only original',))
        self.db.commit()
        for name in ['memory_store.db', 'response_store.db', 'projects.db', 'verification_evidence.db', 'profiles/other/state.db']:
            target = self.home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(target) as db:
                db.execute('create table records (value text)')
                db.execute('insert into records values ("preserve")')
        self.launcher = executable(self.home / 'bin/hermes-fixture', '#!/bin/bash\ncase "$1" in\n--version) echo fixture-before;;\nupdate) [ "${FAIL_UPDATE:-0}" != 1 ] || exit 7; echo update-fixture;;\nwebapp) [ "${FAIL_BUILD:-0}" != 1 ] || exit 8;;\n*) exit 0;;\nesac\n')
        self.env = {**os.environ, 'HERMES_HOME': str(self.home), 'AGENT_DIR': str(self.code),
                    'HERMES_BIN': str(self.launcher), 'HEALTH_SCRIPT': str(ADDON / 'addon_health.py'),
                    'HEALTH_ATTEMPTS': '1', 'HEALTH_INTERVAL': '0', 'WEBAPP_PORT': '1'}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_update(self, *args, **env):
        return subprocess.run(['bash', str(ADDON / 'hermes-safe-update.sh'), *args],
                              env={**self.env, **env}, text=True, capture_output=True, timeout=30)

    def snapshot(self):
        result = self.run_update('--snapshot-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(result.stdout.strip())

    def test_snapshot_captures_wal_data_and_expanded_dynamic_selection(self):
        snap = self.snapshot()
        for name, value in self.saved.items():
            self.assertEqual((snap / name).read_text(), value, name)
        self.assertFalse((snap / 'installs/fixture/environments').exists())
        with sqlite3.connect(snap / 'state.db') as db:
            self.assertEqual(db.execute('pragma integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('select text from messages').fetchall(), [('WAL-only original',)])

    def test_rollback_restores_personal_data_plugins_auth_sqlite_and_classic_chats(self):
        snap = self.snapshot()
        self.db.close()
        self.db = sqlite3.connect(':memory:')
        for name in self.saved:
            (self.home / name).write_text('changed')
        with sqlite3.connect(self.home / 'state.db') as db:
            db.execute('delete from messages')
        result = self.run_update('--rollback', str(snap))
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, content in self.saved.items():
            self.assertEqual((self.home / name).read_text(), content, name)
        for name in ['state.db', 'memory_store.db', 'response_store.db', 'projects.db', 'verification_evidence.db', 'profiles/other/state.db']:
            with sqlite3.connect(self.home / name) as db:
                self.assertEqual(db.execute('pragma integrity_check').fetchone()[0], 'ok', name)
        with sqlite3.connect(self.home / 'state.db') as db:
            self.assertEqual(db.execute('select text from messages').fetchall(), [('WAL-only original',)])
        self.assertFalse((self.home / '.update-running').exists())
        self.assertTrue((self.home / 'plugins/github-copilot/plugin.yaml').exists())

    def test_wiki_git_history_survives_snapshot_and_rollback(self):
        wiki = self.home / 'wiki'
        for args in (['init', '-q'], ['config', 'user.email', 'test@example.invalid'],
                     ['config', 'user.name', 'Test'], ['add', '.'], ['commit', '-qm', 'wiki history']):
            subprocess.run(['git', '-C', str(wiki), *args], check=True, capture_output=True)
        commit = subprocess.check_output(['git', '-C', str(wiki), 'rev-parse', 'HEAD'], text=True).strip()
        snap = self.snapshot()
        (wiki / 'index.md').write_text('changed')
        result = self.run_update('--rollback', str(snap))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(subprocess.check_output(['git', '-C', str(wiki), 'rev-parse', 'HEAD'], text=True).strip(), commit)
        self.assertEqual((wiki / 'index.md').read_text(), self.saved['wiki/index.md'])

    def test_corrupt_snapshot_refuses_restore_without_touching_data(self):
        snap = self.snapshot()
        (snap / 'state.db').write_bytes(b'corrupt')
        result = self.run_update('--rollback', str(snap))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.home / 'auth.json').read_text(), self.saved['auth.json'])
        self.assertFalse((self.home / '.update-running').exists())

    def test_update_command_failure_is_not_reported_as_success(self):
        result = self.run_update('--now', FAIL_UPDATE='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('updater exited 7', result.stderr)
        self.assertFalse((self.home / '.update-pending').exists())
        self.assertFalse((self.home / '.update-running').exists())

    def test_updater_soft_failure_with_zero_exit_is_rolled_back(self):
        self.launcher.write_text('#!/bin/sh\ncase "$1" in --version) echo fixture-before;; update) echo "dependency install failed";; esac\n')
        result = self.run_update('--now')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('incomplete dependency installation', result.stderr)
        self.assertIn('rollback restored', result.stderr)
        self.assertFalse((self.home / '.update-running').exists())

    def test_build_failure_rolls_back_and_releases_pause(self):
        result = self.run_update('--now', FAIL_BUILD='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Webapp build failed', result.stderr)
        self.assertFalse((self.home / '.update-running').exists())

    def test_pending_update_fails_api_only_and_restores_snapshot(self):
        snap = self.snapshot()
        (self.home / 'auth.json').write_text('changed')
        (self.home / '.update-pending').write_text(str(snap) + '\nbefore\n')
        with server('missing') as svc:
            result = self.run_update('--verify', WEBAPP_PORT=str(svc.server_port), API_PORT=str(svc.server_port))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.home / 'auth.json').read_text(), self.saved['auth.json'])
        self.assertFalse((self.home / '.update-pending').exists())
        self.assertFalse((self.home / '.update-running').exists())

    def test_successful_update_waits_for_real_http_bundle_without_optional_api(self):
        with server('ready') as svc:
            result = self.run_update('--now', WEBAPP_PORT=str(svc.server_port))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Hermes updated:', result.stderr)
        self.assertFalse((self.home / '.update-pending').exists())

    def test_failed_sqlite_snapshot_aborts_before_update(self):
        self.db.close()
        self.db = sqlite3.connect(':memory:')
        (self.home / 'state.db').write_bytes(b'not a database')
        result = self.run_update('--now')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('snapshot failed', result.stderr)
        self.assertFalse((self.home / '.update-running').exists())

    def test_home_scoped_gateway_stop_leaves_other_home_alive(self):
        # Real OS processes, with exact argv pairs and different homes.
        sleeper = executable(self.home / 'bin/process-fixture', '#!/usr/bin/python3\nimport time\ntime.sleep(30)\n')
        target = subprocess.Popen([str(sleeper), 'gateway', 'run'], env=self.env)
        other = subprocess.Popen([str(sleeper), 'gateway', 'run'], env={**self.env, 'HERMES_HOME': str(self.home / 'other')})
        try:
            result = self.run_update('--now', FAIL_UPDATE='1')
            self.assertNotEqual(result.returncode, 0)
            target.wait(timeout=5)
            self.assertIsNone(other.poll(), 'must not stop another home')
        finally:
            for process in (target, other):
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
