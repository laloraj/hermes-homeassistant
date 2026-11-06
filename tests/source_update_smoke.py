#!/usr/bin/env python3
"""Exact authenticated Settings request, offline origin and real source-check Git.

Run with the image's Hermes Python and PYTHONPATH pointing to its mirrored source.
Only fixture configuration/release-network boundaries are patched in this separate
process; no Hermes files, UI, source-check probes or downgrade guards are changed.
"""
import contextlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
from unittest.mock import patch
from git_update_fixture import PartialClone


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / 'home'
        home.mkdir()
        os.environ['HERMES_HOME'] = str(home)
        os.environ['HOME'] = str(home)
        os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
        os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
        os.environ.pop('HERMES_REVISION', None)
        fixture = PartialClone(root)
        (home / 'config.yaml').write_text('updates:\n  channel: stable\n')
        with sqlite3.connect(home / 'state.db') as db:
            db.execute('CREATE TABLE session_admissions (id INTEGER PRIMARY KEY)')
            db.execute('CREATE TABLE worker_executions (id INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO session_admissions VALUES (1)')
        from hermes_cli import config, source_check, web_server
        from hermes_cli.source_releases import SourceTarget
        import uvicorn
        # No remote API/network responses are invented as observed upstream data:
        # these are explicit fixture inputs for a newer, then a refused release.
        target = SourceTarget('stable', 'stable', 'fixture/offline', commit=fixture.target,
                              version='99.0.0', build_id='offline-test')
        fixture.run('show', f'{fixture.old}:hermes_state.py', cwd=fixture.clone)
        env_before = fixture.objects()
        trace = root / 'requests.trace'
        os.environ['GIT_TRACE'] = str(trace)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(web_server.app, host='127.0.0.1', port=port,
                                               lifespan='off', log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(config, 'get_project_root', return_value=fixture.clone))
            stack.enter_context(patch.object(web_server, 'PROJECT_ROOT', fixture.clone))
            stack.enter_context(patch.object(web_server, 'detect_install_method', return_value='git', create=True))
            stack.enter_context(patch.object(web_server, '_dashboard_local_update_managed_externally', return_value=False, create=True))
            stack.enter_context(patch.object(web_server, '_config_profile_scope', side_effect=lambda _: contextlib.nullcontext(), create=True))
            resolver = stack.enter_context(patch.object(source_check, 'resolve_source_target', return_value=target))
            schema = stack.enter_context(patch.object(source_check, '_github_predates_runtime', return_value=False))
            # The fixture must never reach an unmocked external HTTP endpoint.
            stack.enter_context(patch.object(source_check, '_request', side_effect=AssertionError('unexpected network')))
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started and time.monotonic() < deadline:
                    time.sleep(.05)
                assert server.started, 'offline Settings HTTP server failed to start'
                for force in (True, False):
                    started = time.monotonic()
                    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/hermes/update/check?force={str(force).lower()}',
                        headers={'X-Hermes-Session-Token': web_server._SESSION_TOKEN})
                    with opener.open(req, timeout=29) as response:
                        assert response.status == 200
                        payload = json.load(response)
                    elapsed = time.monotonic() - started
                    assert elapsed < 30, f'Settings request exceeded browser budget: {elapsed:.2f}s'
                    assert payload['behind'] == -1 and payload['update_available'] is True, payload
                    assert payload['can_apply'] is True, payload
                    assert fixture.objects() == env_before, 'Settings checks fetched missing target objects'
                    assert 'fetch ' not in trace.read_text(), 'Settings checks launched a lazy fetch'
                    print(f'PASS: Settings {"forced" if force else "cached"} HTTP {elapsed:.3f}s; update available, unknown count')
                assert resolver.call_count == 1, 'cached request resolved the remote release again'
                assert schema.call_count == 2, 'downgrade guard did not run on both fresh and cached checks'
                # Cached source facts must not bypass a changed refusal verdict.
                schema.return_value = True
                req = urllib.request.Request(f'http://127.0.0.1:{port}/api/hermes/update/check',
                    headers={'X-Hermes-Session-Token': web_server._SESSION_TOKEN})
                with opener.open(req, timeout=29) as response:
                    payload = json.load(response)
                assert payload['behind'] == 0 and not payload['update_available'] and payload['held'] == 'downgrade', payload
                assert fixture.objects() == env_before, 'Settings checks fetched missing target objects'
                assert 'fetch ' not in trace.read_text(), 'Settings checks launched a lazy fetch'
                # Real locally complete schema controls: old schema refuses, a
                # forward target stays allowed, with the actual upstream guard.
                from hermes_cli.update_downgrade_guard import would_refuse
                def git(*args):
                    result = source_check._git_run(list(args), cwd=fixture.clone, timeout=5)
                    return -1 if result is None else result.returncode
                assert would_refuse(fixture.clone, fixture.old, home, git=git), 'old local schema escaped downgrade refusal'
                assert not would_refuse(fixture.clone, fixture.head, home, git=git), 'current runtime was refused'
                print('PASS: cached downgrade protection and local old/forward schema controls; no lazy fetch')
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive(), 'fixture server did not stop'


if __name__ == '__main__':
    main()
