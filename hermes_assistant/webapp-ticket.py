#!/usr/bin/env python3
"""Front door for the Hermes Webapp behind HA Ingress.

HA login is the gate: anyone who reaches this helper already passed it (the add-on publishes no ports).
Opening the add-on panel hits `/`; nginx hands that request here. We mint a one-use Webapp sign-in link
(`hermes webapp --no-open`) and redirect the browser into the Webapp, so there is no button and no sign-in page.

If the Webapp is not ready (still building, disabled, or the agent predates it) we send the browser to the
old WebUI at /old/ instead, or show a short "building" page, so the panel never dead-ends.
"""
import os
import re
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_PORT = int(os.environ.get("TICKET_PORT", "9130"))
WEBAPP_PORT = int(os.environ.get("WEBAPP_PORT", "9120"))
HERMES_BIN = os.environ.get("HERMES_BIN", "/usr/local/bin/hermes")
HERMES_HOME = os.environ.get("HERMES_HOME", "/data/hermes")
BUILDING_FLAG = os.path.join(HERMES_HOME, ".webapp-building")
TICKET_RE = re.compile(r"ticket=([A-Za-z0-9_-]{43})")
_mint_lock = threading.Lock()

BUILDING_PAGE = """<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>Hermes</title><body style="font:16px sans-serif;margin:3em;color:#ddd;background:#111">
<p>The Hermes Webapp is still being prepared (first build takes a few minutes). This page retries every 10 seconds.</p>
<p><a style="color:#8cf" href="{old}">Open the classic WebUI meanwhile</a></p></body>"""


def webapp_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", WEBAPP_PORT), timeout=2):
            return True
    except OSError:
        return False


def mint_ticket():
    """A fresh one-use sign-in ticket, or None."""
    with _mint_lock:
        try:
            out = subprocess.run([HERMES_BIN, "webapp", "--no-open", "--skip-build"], capture_output=True,
                                 text=True, timeout=60, env={**os.environ, "HERMES_HOME": HERMES_HOME}).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    m = TICKET_RE.search(out)
    return m.group(1) if m else None


class Handler(BaseHTTPRequestHandler):
    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        prefix = (self.headers.get("X-Ingress-Path") or "").rstrip("/")
        old = prefix + "/old/"
        if webapp_up():
            ticket = mint_ticket()
            if ticket:
                return self._redirect("%s/webapp/webapp/launch#ticket=%s" % (prefix, ticket))
        if os.path.exists(BUILDING_FLAG):
            body = BUILDING_PAGE.format(old=old).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._redirect(old)   # Webapp unavailable: fall back to the classic WebUI

    do_HEAD = do_GET

    def log_message(self, format, *args):  # noqa: A002 - signature required by BaseHTTPRequestHandler
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()
