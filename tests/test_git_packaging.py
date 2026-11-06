import pathlib
import subprocess
import tempfile
import unittest
import yaml
from git_update_fixture import PartialClone

ROOT = pathlib.Path(__file__).resolve().parents[1]


class GitPackaging(unittest.TestCase):
    def test_both_image_paths_ship_same_isolated_checksum_verified_git(self):
        dockerfile = (ROOT / 'hermes_assistant/Dockerfile').read_text()
        workflow = (ROOT / '.github/workflows/retag.yml').read_text()
        script = ROOT / 'hermes_assistant/build-git.sh'
        self.assertTrue(script.exists(), 'missing shared pinned Git builder')
        text = script.read_text()
        self.assertRegex(text, r'GIT_VERSION=2\.\d+\.\d+')
        self.assertRegex(text, r'GIT_SHA256=[0-9a-f]{64}')
        self.assertIn('sha256sum -c', text)
        self.assertIn('libcurl4-openssl-dev', text)
        for image in (dockerfile, workflow):
            self.assertIn('AS git-builder', image)
            self.assertIn('COPY build-git.sh /build-git.sh', image)
            self.assertIn('RUN sh /build-git.sh', image)
            self.assertIn('COPY --from=git-builder /opt/git /opt/git', image)
            self.assertIn('/opt/git/bin/git /usr/local/bin/git', image)
            self.assertIn('libcurl4', image)
            self.assertNotIn('trixie', image)
        self.assertIn('"hermes_assistant/build-git.sh"', workflow)

    def test_retag_printf_generates_two_correct_architecture_stages(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/retag.yml').read_text())
        run = next(step['run'] for step in workflow['jobs']['certify']['steps']
                   if 'run' in step and 'Dockerfile.patch' in step['run'])
        command = next(line.strip() for line in run.splitlines() if line.strip().startswith('printf '))
        for arch in ('amd64', 'aarch64'):
            with tempfile.TemporaryDirectory() as directory:
                subprocess.run(['bash', '-ec', command], cwd=directory,
                               env={'PATH': '/usr/bin:/bin', 'arch': arch, 'UPSTREAM_TAG': '1.6.8'}, check=True)
                image = pathlib.Path(directory, 'Dockerfile.patch').read_text()
                base = f'FROM ghcr.io/nuttaruj/{arch}-hermes-assistant:1.6.8'
                self.assertEqual([line for line in image.splitlines() if line.startswith('FROM ')],
                                 [base + ' AS git-builder', base])
                self.assertIn('RUN sh /build-git.sh\n', image)
                self.assertIn('COPY --from=git-builder /opt/git /opt/git\n', image)

    def test_installed_git_passive_probes_never_lazy_fetch(self):
        # Native CI enforces this test with the built Git inside image_boot.py.
        # Host 2.39 intentionally remains supported for the wrapper-only suite.
        import os
        if os.environ.get('HERMES_GIT_SMOKE') != '1':
            self.skipTest('real Git capability gate requires HERMES_GIT_SMOKE=1')
        with tempfile.TemporaryDirectory() as root:
            PartialClone(root).passive_probes()
