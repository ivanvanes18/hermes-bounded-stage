import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import routing_fixtures as F
import stage_cli
from schema_validation import digest
class CLITests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((SCRIPTS/'route_cli.py').is_file(),'routing CLI missing')
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
        self.s=F.stage(self.root);self.reg=F.registry();F.attach_adapter(self.root,self.reg)
        self.sp=self.root/'stage.json';self.sp.write_text(json.dumps(self.s))
        self.rp=self.root/'registry.json';self.rp.write_text(json.dumps(self.reg))
        self.run=self.root/'run'
    def call(self,*args):
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):code=stage_cli.main(list(args))
        return code,json.loads(buf.getvalue())
    def initargs(self):return ['--stage',str(self.sp),'--registry',str(self.rp),'--registry-sha256',F.sha(self.rp)]
    def test_doctor_no_live_claim(self):
        code,r=self.call('route-doctor');self.assertEqual(code,0);self.assertFalse(r['live_readiness_claim'])
    def test_validate_stage(self):
        code,r=self.call('route-validate',*self.initargs());self.assertEqual(code,0);self.assertEqual(r['status'],'valid')
    def test_bad_registry_hash(self):
        args=self.initargs();args[-1]='0'*64;code,r=self.call('route-validate',*args)
        self.assertEqual(code,2);self.assertEqual(r['status'],'blocked')
    def test_full_cli_fixed_worker_and_parent_accept(self):
        code,r=self.call('route-init',*self.initargs(),'--run',str(self.run),'--evidence-mode','synthetic');self.assertEqual(code,0)
        code,r=self.call('route-advance','--run',str(self.run),'--fixed-route','fixture_worker');self.assertEqual(code,0)
        self.assertEqual(r['status'],'ready_for_parent_review');self.assertFalse(r['parent_accepted'])
        approval=self.root/'approval.json';approval.write_text(json.dumps({'decision':'accept','reviewer':'synthetic-parent',
          'evidence_hash':r['result_hash'],'evidence_kind':'synthetic'}))
        code,r=self.call('route-accept','--run',str(self.run),'--approval',str(approval));self.assertEqual(code,0);self.assertTrue(r['parent_accepted'])
    def test_network_flag_without_grant_blocks(self):
        self.call('route-init',*self.initargs(),'--run',str(self.run),'--evidence-mode','synthetic')
        code,r=self.call('route-advance','--run',str(self.run),'--allow-typesafe');self.assertEqual(code,2)
    def test_missing_arguments_json_error(self):
        code,r=self.call('route-validate');self.assertEqual(code,2);self.assertEqual(r['status'],'blocked')
    def test_candidate_version(self):
        import bounded_runtime,install_skill
        self.assertEqual(bounded_runtime.SKILL_VERSION,'1.2.0')
        self.assertEqual(install_skill.VERSION,'1.2.0')
        self.assertIn('version: 1.2.0',(SCRIPTS.parent/'SKILL.md').read_text())
if __name__=='__main__':unittest.main()
