import pathlib,subprocess,unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]
class ReleasePublication(unittest.TestCase):
 def test_only_main_push_publishes_certified_images(self):
  workflow=(ROOT/'.github/workflows/retag.yml').read_text()
  tail=workflow.split('          # Neither architecture is published until BOTH fresh-boot smokes pass.',1)[1]
  shell='\n'.join(line[10:] if line.startswith('          ') else line for line in tail.splitlines())
  for event,ref,expected in [('workflow_dispatch','refs/heads/webapp-only-release',0),('workflow_dispatch','refs/heads/main',0),('push','refs/heads/webapp-only-release',0),('push','refs/heads/main',4)]:
   with self.subTest(event=event,ref=ref):
    program=shell.replace('${{ github.event_name }}',event).replace('${{ github.ref }}',ref)
    result=subprocess.run(['bash','-ec','VERSION=1.10.0; docker() { printf "%s\\n" "$*"; }; '+program],capture_output=True,text=True,check=True)
    lines=result.stdout.splitlines();self.assertEqual(len(lines),expected)
    if expected:self.assertTrue(all(line.startswith('push ghcr.io/laloraj/') for line in lines))
