import pathlib,subprocess,unittest
import yaml
ROOT=pathlib.Path(__file__).resolve().parents[1]
class ReleasePublication(unittest.TestCase):
 def workflow(self):return yaml.safe_load((ROOT/'.github/workflows/retag.yml').read_text())
 def test_both_images_use_upstream_frozen_dependency_overrides(self):
  for path in ['hermes_assistant/Dockerfile','.github/workflows/retag.yml']:
   text=(ROOT/path).read_text()
   self.assertIn('uv sync --project /opt/hermes-agent-code --frozen --no-dev --extra all --extra messaging',text)
   self.assertNotIn('uv pip install --python /opt/hermes-agent-code/venv/bin/python',text)
   self.assertIn('libatomic1',text)
 def test_native_architecture_certification_and_both_arch_gate(self):
  jobs=self.workflow()['jobs'];certify=jobs['certify'];publish=jobs['publish']
  self.assertEqual(certify['runs-on'],'${{ matrix.runner }}')
  runners={r['arch']:r['runner'] for r in certify['strategy']['matrix']['include']}
  self.assertEqual(runners,{'amd64':'ubuntu-24.04','aarch64':'ubuntu-24.04-arm'})
  self.assertEqual(publish['needs'],'certify')
  self.assertEqual(publish['if'],"github.event_name == 'push' && github.ref == 'refs/heads/main'")
  run=next(s['run'] for s in certify['steps'] if 'run'in s and '/tests/image_boot.py' in s['run'])
  self.assertLess(run.index('/tests/image_boot.py'),run.index('docker push'))
  self.assertNotIn(':latest',run)
  self.assertNotIn('docker/setup-qemu-action',(ROOT/'.github/workflows/retag.yml').read_text())
 def test_publish_promotes_exact_certified_candidates(self):
  shell=self.workflow()['jobs']['publish']['steps'][-1]['run']
  shell=shell.replace("VERSION=$(yq eval '.version' hermes_assistant/config.yaml)",'VERSION=1.10.0').replace('${{ github.sha }}','verified-sha')
  result=subprocess.run(['bash','-ec','docker() { printf "%s\\n" "$*"; }; '+shell],capture_output=True,text=True,check=True)
  creates=[l for l in result.stdout.splitlines() if 'imagetools create'in l]
  self.assertEqual(len(creates),2)
  for arch,line in zip(['amd64','aarch64'],creates):
   image='ghcr.io/laloraj/'+arch+'-hermes-assistant'
   self.assertIn('--tag '+image+':1.10.0 --tag '+image+':latest '+image+':ci-verified-sha',line)
