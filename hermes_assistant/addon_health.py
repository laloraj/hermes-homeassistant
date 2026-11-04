#!/usr/bin/env python3
"""Bounded HTTP readiness for the gateway-hosted Webapp (stdlib only).

Strict --ready never accepts grace. --watchdog allows a finite initial startup
or update window, but never hides a failure after the first healthy boot.
No inference call or optional HA/Bitwarden/API-server dependency is required.
"""
import argparse
import json
import os
from pathlib import Path
import re
import time
import urllib.request


def fetch(port, path, limit=1048576):
    # Ignore proxy environment: these requests must remain on loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:%s%s' % (port, path), timeout=2) as response:
        if response.status != 200:
            raise ValueError('HTTP not ready')
        return response.read(limit), response.headers.get('Content-Type', '')


def readiness(home=None, port=None):
    home = Path(home or os.environ.get('HERMES_HOME', '/data/hermes'))
    port = int(port or os.environ.get('WEBAPP_PORT', '9120'))
    result = {'ready': False, 'gateway': False, 'webapp': False, 'state': 'unavailable'}
    try:
        data, _ = fetch(port, '/api/health')
        result['gateway'] = json.loads(data).get('ok') is True
        if not result['gateway']:
            return result
        data, _ = fetch(port, '/api/status')
        status = json.loads(data)
        if status.get('gateway_running') is not True or status.get('gateway_state') != 'running':
            result.update(gateway=False, state='starting')
            return result
        data, kind = fetch(port, '/')
        html = data.decode('utf-8')
        # The built Webapp, not a TCP listener, login page or generic nginx 200.
        asset = re.search(r'<script\b[^>]*\bsrc=[\"\']((?:\./|/)assets/[A-Za-z0-9_./-]+\.js)[\"\']', html)
        if 'text/html' not in kind or '__HERMES_UI_SURFACE__="webapp"' not in html or not asset:
            return result
        body, kind = fetch(port, '/' + asset.group(1).lstrip('./'), 1)
        if not body or 'javascript' not in kind:
            return result
        result.update(ready=True, webapp=True, state='ready')
    except (OSError, ValueError, UnicodeError, AttributeError, TypeError):
        pass
    if (home / '.webapp-building').exists():
        result['state'] = 'building'
    elif (home / '.webapp-build-failed').exists():
        result['state'] = 'build-failed'
    if result['ready']:
        result['state'] = 'ready'
    return result


def watchdog(state, home=None, now=None, grace=None):
    home = Path(home or os.environ.get('HERMES_HOME', '/data/hermes'))
    now = time.time() if now is None else now
    grace = int(os.environ.get('STARTUP_GRACE_SECONDS', '900')) if grace is None else grace
    if state['ready']:
        (home / '.addon-ready').touch()
        return True
    markers = ['.update-running', '.update-pending']
    if not (home / '.addon-ready').exists():
        markers.append('.addon-started')
    for name in markers:
        try:
            age = now - (home / name).stat().st_mtime
            if 0 <= age < grace:
                return True
        except OSError:
            pass
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ready', action='store_true')
    parser.add_argument('--watchdog', action='store_true')
    args = parser.parse_args()
    state = readiness()
    ok = watchdog(state) if args.watchdog else state['ready']
    print(json.dumps({**state, 'healthy': ok}))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
