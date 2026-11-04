#!/usr/bin/env python3
"""HA Ingress front door; wrapper recovery is independent of the Hermes runtime."""
import html
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from addon_health import readiness, watchdog

LISTEN_PORT = int(os.environ.get('TICKET_PORT', '9130'))
HERMES_BIN = os.environ.get('HERMES_BIN', '/usr/local/bin/hermes')
HERMES_HOME = os.environ.get('HERMES_HOME', '/data/hermes')
TICKET_RE = re.compile(r'ticket=([A-Za-z0-9_-]{43})(?![A-Za-z0-9_-])')
_mint_lock = threading.Lock()


def mint_ticket():
    # Do not let parallel panel opens queue indefinitely behind a stuck CLI.
    if not _mint_lock.acquire(timeout=1):
        return None
    try:
        result = subprocess.run([HERMES_BIN, 'webapp', '--no-open', '--skip-build'],
                                capture_output=True, text=True, timeout=30,
                                env={**os.environ, 'HERMES_HOME': HERMES_HOME})
        if result.returncode:
            return None
        match = TICKET_RE.search(result.stdout)
        return match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        _mint_lock.release()


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body=b'', kind='text/html; charset=utf-8', location=None):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Length', str(len(body)))
        if location:
            self.send_header('Location', location)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/health':
            state = readiness()
            healthy = watchdog(state)
            import json
            return self.respond(200 if healthy else 503,
                                json.dumps({**state, 'healthy': healthy}).encode(), 'application/json')
        if path not in ('/', '/ready'):
            return self.respond(404, b'Not found', 'text/plain')
        state = readiness()
        if path == '/ready':
            import json
            return self.respond(200 if state['ready'] else 503, json.dumps(state).encode(), 'application/json')
        prefix = (self.headers.get('X-Ingress-Path') or '').rstrip('/')
        # Never turn a forged header into an open redirect or injected HTML.
        if prefix and not re.fullmatch(r'/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+', prefix):
            return self.respond(400, b'Invalid ingress prefix', 'text/plain')
        if state['ready'] and self.command != 'HEAD':
            ticket = mint_ticket()
            if ticket:
                return self.respond(302, location=prefix + '/webapp/webapp/launch#ticket=' + ticket)
        message = 'Webapp sign-in is unavailable.' if state['ready'] else 'Hermes Webapp: ' + state['state'] + '.'
        # A wrapper-only landing page, not another Hermes UI. Static recovery also works if this helper dies.
        body = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<meta http-equiv="refresh" content="10"><title>Hermes recovery</title>'
                '<h1>Hermes is not ready</h1><p>' + html.escape(message) + '</p>'
                '<p>Retrying in 10 seconds. Startup/build can take several minutes.</p>'
                '<p><a href="' + prefix + '/recovery/">Recovery: terminal, logs and rollback</a></p>').encode()
        self.respond(200, body)

    do_HEAD = do_GET

    def log_message(self, format, *args):
        # Request URLs can contain credentials. Do not log them.
        pass


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', LISTEN_PORT), Handler).serve_forever()
