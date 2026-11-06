"""Offline real-Git regression shared by local checks and disposable images."""
import hashlib
import os
from pathlib import Path
import subprocess


def git_env(home):
    return {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
            'HOME': str(home), 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
            'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
            'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}


class PartialClone:
    def __init__(self, root):
        self.root = Path(root)
        self.env = git_env(root)
        self.seed, self.origin, self.clone = [self.root / p for p in ('seed', 'origin.git', 'clone')]
        self.run('init', '-q', '-b', 'main', str(self.seed))
        (self.seed / 'hermes_state.py').write_text('# pre-runtime release\n')
        self.run('add', '.', cwd=self.seed)
        self.run('commit', '-qm', 'old schema', cwd=self.seed)
        self.old = self.run('rev-parse', 'HEAD', cwd=self.seed)
        (self.seed / 'hermes_state.py').write_text('CREATE TABLE IF NOT EXISTS session_admissions\n')
        self.run('commit', '-qam', 'runtime schema', cwd=self.seed)
        self.run('clone', '-q', '--bare', str(self.seed), str(self.origin))
        self.run('config', 'uploadpack.allowFilter', 'true', cwd=self.origin)
        self.run('config', 'uploadpack.allowAnySHA1InWant', 'true', cwd=self.origin)
        self.run('clone', '-q', '--filter=blob:none', '--no-checkout', self.origin.as_uri(), str(self.clone))
        self.head = self.run('rev-parse', 'HEAD', cwd=self.clone)
        (self.seed / 'new.txt').write_text('unfetched upstream commit\n')
        self.run('add', '.', cwd=self.seed)
        self.run('commit', '-qm', 'upstream moved', cwd=self.seed)
        self.run('push', '-q', str(self.origin), 'main', cwd=self.seed)
        self.target = self.run('rev-parse', 'HEAD', cwd=self.seed)
        assert self.run('config', '--get', 'remote.origin.promisor', cwd=self.clone) == 'true'
        assert self.run('config', '--get', 'remote.origin.partialclonefilter', cwd=self.clone) == 'blob:none'
        # Populate the checked-out blobs, not the new target, for clean status probes.
        self.run('checkout', '-q', 'main', cwd=self.clone)

    def run(self, *args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd, env=self.env,
                                       stderr=subprocess.PIPE, text=True).strip()

    def objects(self):
        return {str(p.relative_to(self.clone)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (self.clone / '.git' / 'objects').rglob('*') if p.is_file()}

    def passive_probes(self):
        before = self.objects()
        trace = self.root / 'passive-git.trace'
        env = {**self.env, 'GIT_NO_LAZY_FETCH': '1', 'GIT_TRACE': str(trace)}
        for args in [('merge-base', '--is-ancestor', 'HEAD', self.target),
                     ('rev-list', '--objects', '--no-walk', '--sparse', '--missing=error',
                      self.target, '--', 'hermes_state*.py')]:
            result = subprocess.run(['git', *args], cwd=self.clone, env=env,
                                    capture_output=True, text=True, timeout=5)
            assert result.returncode != 0, 'missing target unexpectedly resolved: Git ignored GIT_NO_LAZY_FETCH'
        assert self.objects() == before, 'passive probes downloaded promisor objects'
        assert 'fetch ' not in trace.read_text(), 'passive probes launched a lazy fetch'
        # Prove the target really exists and the fixture transport can fetch it.
        result = subprocess.run(['git', 'cat-file', '-e', self.target], cwd=self.clone,
                                env=self.env, capture_output=True, timeout=5)
        assert result.returncode == 0, 'fixture positive control cannot lazy-fetch target'
        assert self.objects() != before, 'positive control did not download objects'
