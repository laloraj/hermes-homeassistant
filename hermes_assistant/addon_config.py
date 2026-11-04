#!/usr/bin/env python3
"""Add-on-owned Webapp endpoint and optional HA MCP settings, atomically written.

Uses the explicitly installed system python3-yaml, never a classic WebUI venv.
Credentials enter via environment, not argv, and are never printed.
"""
import argparse
import os
from pathlib import Path
import tempfile
import yaml


def configure(path, mcp=False, hass_url='', token=''):
    path = Path(path)
    cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError('Hermes configuration must be a mapping')
    webapp = cfg.setdefault('webapp', {})
    if not isinstance(webapp, dict):
        raise ValueError('Webapp configuration must be a mapping')
    # The wrapper owns this loopback endpoint: nginx and readiness use it.
    webapp.update(enabled=True, host='127.0.0.1', port=9120)
    if mcp and hass_url and token:
        servers = cfg.setdefault('mcp_servers', {})
        if isinstance(servers, dict):
            servers['homeassistant'] = {
                'url': hass_url.rstrip('/') + '/api/mcp',
                'headers': {'Authorization': 'Bearer ' + token},
            }
        else:
            print('[addon-config] Optional MCP section is not a mapping; left unchanged.')
    fd, temporary = tempfile.mkstemp(prefix='.addon-config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            yaml.safe_dump(cfg, stream, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path')
    parser.add_argument('--mcp', action='store_true')
    args = parser.parse_args()
    try:
        configure(args.path, args.mcp, os.environ.get('HASS_URL', ''), os.environ.get('HASS_TOKEN', ''))
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        print('[addon-config] Configuration was not changed; check YAML in the recovery terminal.')
        return 1
    print('[addon-config] Webapp loopback endpoint configured; optional integrations preserved.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
