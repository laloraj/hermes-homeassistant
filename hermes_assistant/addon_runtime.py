#!/usr/bin/python3
"""Portable PM intent and supported cold bootstrap; never copy managed binaries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def selection(home, source):
    key = hashlib.sha256(str(Path(source).resolve()).encode()).hexdigest()[:16]
    return Path(home) / 'installs' / key


def validate(recipe):
    if recipe.get('schema') != 1:
        raise ValueError('unsupported PM intent schema')
    for key in ('extras', 'declined_extras'):
        values = recipe[key]
        if not isinstance(values, list) or any(not isinstance(v, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', v) for v in values):
            raise ValueError('invalid PM extras intent')
    for name, version in recipe['tool_versions'].items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or not isinstance(version, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.+_*~-]{0,127}', version):
            raise ValueError('invalid PM tool intent')
    for name, digest in recipe['source_input_sha256'].items():
        if name not in ('pyproject.toml', 'uv.lock', 'pm/lock.json') or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('invalid PM source intent')
    return recipe


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.pm-intent-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def export_runtime_recipe(home, source, target):
    """Same schema as the backups engine: portable extras/declines/versions/hashes."""
    home, source, target = map(Path, (home, source, target))
    state = selection(home, source)
    if not (state / 'facts.json').exists():
        existing = home / 'pm-reconstruction.json'
        if existing.exists():
            atomic_json(target, validate(load(existing)))
            return True
        return False  # Legacy pip install: no PM selection exists yet.
    facts = load(state / 'facts.json')
    declined = state / 'declined-extras.json'
    tools = home / 'tools/facts.json'
    recipe = {'schema': 1, 'extras': sorted(set(facts['packages']['venv']['extras'])),
              'declined_extras': sorted(set(load(declined).get('declined', []))) if declined.exists() else [],
              'tool_versions': {k: v['version'] for k, v in load(tools)['packages'].items() if v.get('version') is not None} if tools.exists() else {},
              'source_input_sha256': {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                                      for name in ('pyproject.toml', 'uv.lock', 'pm/lock.json') if (source / name).is_file()}}
    atomic_json(target, validate(recipe))
    return True


def ensure_runtime(home, source, bootstrap):
    home, source = Path(home), Path(source)
    recipe_path = home / 'pm-reconstruction.json'
    if not recipe_path.exists():
        export_runtime_recipe(home, source, recipe_path)
    if not recipe_path.exists():
        # A relocated seed/legacy venv can be used without PM only on modern Python.
        python = source / 'venv/bin/python'
        result = subprocess.run([str(python), '-c', 'import sys; sys.exit(sys.version_info < (3,12))'], check=False) if python.exists() else None
        if result is not None and result.returncode == 0:
            return
        recipe = {'schema': 1, 'extras': ['all', 'messaging'], 'declined_extras': [],
                  'tool_versions': {}, 'source_input_sha256': {}}
        atomic_json(recipe_path, recipe)
    recipe = validate(load(recipe_path))
    for name, digest in recipe['source_input_sha256'].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError('PM intent does not match restored source; restore matching revision first')
    lock = load(source / 'pm/lock.json')
    if any(lock['packages'][name]['version'] != version for name, version in recipe['tool_versions'].items()):
        raise ValueError('PM tool intent does not match source lock')
    state = selection(home, source)
    if (state / 'facts.json').exists():
        selected = load(state / 'facts.json')['packages']['venv']
        environment = Path(selected.get('environment', '/nonexistent'))
        tools_ready = not recipe['tool_versions']
        if (home / 'tools/facts.json').exists():
            installed = load(home / 'tools/facts.json')['packages']
            tools_ready = all(installed.get(name, {}).get('version') == version and
                              (home / 'tools' / installed[name].get('entry', '__missing__')).exists()
                              for name, version in recipe['tool_versions'].items())
        if (environment.is_relative_to(home)
                and (environment / 'bin/python').exists() and (source / '.hermes/bin/hermes').exists() and tools_ready):
            # Keep legitimate later PM extras/decline changes; intent is not a permanent pin.
            export_runtime_recipe(home, source, recipe_path)
            return
    # PM's tar extraction requires a modern bootstrap. Never patch out its filters.
    subprocess.run([bootstrap, '-c', 'import sys; sys.exit(sys.version_info < (3,12))'], check=True)
    env = {k: v for k, v in os.environ.items() if k not in ('VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONHOME') and not k.startswith('HERMES_PM_')}
    env.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(home / 'tools'))
    def command(args):
        subprocess.run([bootstrap, '-m', 'pm.cli', 'install', *args], cwd=source, env=env, check=True, timeout=1200)
    if recipe['tool_versions']:
        command(sorted(recipe['tool_versions']))
    state.mkdir(parents=True, exist_ok=True)
    atomic_json(state / 'declined-extras.json', {'schema': 1, 'declined': recipe['declined_extras']})
    command([flag for extra in recipe['extras'] for flag in ('--extra', extra)])
    selected = load(state / 'facts.json')['packages']['venv']
    if sorted(selected['extras']) != sorted(recipe['extras']):
        raise ValueError('PM rebuilt extras do not match intent')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('export', 'ensure'))
    parser.add_argument('--home', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--target')
    parser.add_argument('--bootstrap', default=os.environ.get('HERMES_BOOTSTRAP_PYTHON', '/opt/hermes-agent-code/venv/bin/python'))
    args = parser.parse_args()
    if args.action == 'export':
        export_runtime_recipe(args.home, args.source, args.target)
    else:
        ensure_runtime(args.home, args.source, args.bootstrap)


if __name__ == '__main__':
    main()
