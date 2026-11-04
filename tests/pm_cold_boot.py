"""Opt-in cold PM integration: source + intent only, no live credentials or venv."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    if os.environ.get('HERMES_PM_SMOKE') != '1':
        raise SystemExit('Run only with HERMES_PM_SMOKE=1; this downloads managed tools/extras.')
    addon = Path(__file__).resolve().parents[1] / 'hermes_assistant'
    spec = importlib.util.spec_from_file_location('addon_runtime', addon / 'addon_runtime.py')
    assert spec is not None and spec.loader is not None
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    original_home = Path(os.environ.get('PM_SOURCE_HOME', '/data/hermes'))
    original_source = original_home / 'agent-code'
    tools = json.loads((original_home / 'tools/facts.json').read_text())
    bootstrap = original_home / 'tools' / tools['packages']['python']['entry'] / 'bin/python3'
    with tempfile.TemporaryDirectory(prefix='addon-pm-cold-', dir=os.environ.get('TMPDIR')) as tmp:
        private = Path(tmp)
        private.chmod(0o700)
        home, source = private / 'home', private / 'home/agent-code'
        source.mkdir(parents=True)
        archive = private / 'source.tar'
        with archive.open('wb') as stream:
            subprocess.run(['git', '-C', str(original_source), 'archive', 'HEAD'], stdout=stream, check=True)
        subprocess.run(['tar', '-xf', str(archive), '-C', str(source)], check=True)
        runtime.export_runtime_recipe(original_home, original_source, home / 'pm-reconstruction.json')
        recipe = json.loads((home / 'pm-reconstruction.json').read_text())
        env = {'HOME': str(home), 'HERMES_HOME': str(home), 'PATH': '/usr/local/bin:/usr/bin:/bin',
               'TMPDIR': str(private), 'UV_CACHE_DIR': str(private / 'uv-cache'),
               'XDG_CACHE_HOME': str(private / 'cache'), 'XDG_CONFIG_HOME': str(private / 'config'),
               'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'LANG': 'C.UTF-8'}
        log = private / 'install.log'
        with log.open('w') as stream:
            result = subprocess.run(['/usr/bin/python3', str(addon / 'addon_runtime.py'), 'ensure',
                                     '--home', str(home), '--source', str(source), '--bootstrap', str(bootstrap)],
                                    env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=1200)
        if result.returncode:
            # Scratch paths/URLs only; no restored user config or auth is present.
            print(log.read_text()[-4000:].replace(str(private), '<cold-home>'))
            raise RuntimeError('cold reconstruction failed')
        state = runtime.selection(home, source)
        selected = json.loads((state / 'facts.json').read_text())['packages']['venv']
        assert sorted(selected['extras']) == recipe['extras']
        installed = json.loads((home / 'tools/facts.json').read_text())['packages']
        assert all(installed[name]['version'] == version for name, version in recipe['tool_versions'].items())
        python = Path(selected['environment']) / 'bin/python'
        assert python.is_relative_to(home)
        subprocess.run([str(python), '-c', 'import hermes_state, hermes_yaml, httpx; import hermes_cli.projects_db'],
                       cwd=source, env=env, check=True, timeout=60)
        # Second run is a fast, no-network no-op against the rebuilt selection.
        with log.open('w') as stream:
            subprocess.run(['/usr/bin/python3', str(addon / 'addon_runtime.py'), 'ensure', '--home', str(home),
                            '--source', str(source), '--bootstrap', '/must-not-be-used/python'],
                           env=env, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=30)
        print(json.dumps({'result': 'PASS', 'extras': len(recipe['extras']), 'tools': len(recipe['tool_versions']),
                          'cold_imports': True, 'second_run_without_bootstrap': True, 'credentials_loaded': False,
                          'live_mutations': False, 'cleanup': 'automatic'}))


if __name__ == '__main__':
    main()
