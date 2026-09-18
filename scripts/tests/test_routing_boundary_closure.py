"""Cross-component contracts missed by isolated registry and runner fixtures."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import routing_fixtures as F
import executor_routes as R
import executor_runtime as E
import stage_contracts as C

class BoundaryClosure(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
        self.s=F.stage(self.root);self.reg=F.registry();F.attach_adapter(self.root,self.reg)
    def test_private_task_never_selects_external_executor(self):
        self.s['data_policy']['classification']='private'
        p=F.ready(self.reg,C.digest(self.s));p['fixture_worker']['adapter_digest']=C.digest(self.reg['routes'][-1]['adapter'])
        self.assertEqual(R.available(self.s,self.reg,p),['owner'])
    def test_public_task_requires_external_authorization(self):
        self.s['data_policy']['classification']='public-redacted'
        p=F.ready(self.reg,C.digest(self.s));p['fixture_worker']['adapter_digest']=C.digest(self.reg['routes'][-1]['adapter'])
        self.assertEqual(R.available(self.s,self.reg,p),['owner'])
    def test_nonfinite_probe_expiry_refused(self):
        p=F.ready(self.reg,C.digest(self.s));p['fixture_worker']['adapter_digest']=C.digest(self.reg['routes'][-1]['adapter']);p['fixture_worker']['expires_at']=float('nan')
        self.assertEqual(R.available(self.s,self.reg,p),['owner'])
    def test_deterministic_without_pinned_action_not_ready(self):
        self.s.update(executor_candidates=['deterministic','owner'],role='transform')
        self.assertEqual(R.available(self.s,self.reg,{}),['owner'])
    def test_deterministic_pinned_action_runs(self):
        p=self.root/'action.py';p.write_text('import pathlib,sys\n(pathlib.Path(sys.argv[1])/"sample.py").write_text("def add(a, b):\\n    return a + b\\n")\n')
        exe=str(Path(sys.executable).resolve())
        self.s.update(executor_candidates=['deterministic','owner'],role='transform',
            deterministic_action={'id':'fixed-transform','argv':[exe,str(p),'{workspace}'],'pins':{exe:F.sha(exe),str(p):F.sha(p)},'timeout_seconds':10,'expected_exit':0})
        E.initialize(self.s,self.reg,self.root/'run',evidence_mode='synthetic')
        r=E.advance(self.root/'run',fixed_route='deterministic')
        self.assertEqual(r['status'],'ready_for_parent_review');self.assertIsNone(r['last_result']['proof'])
        self.assertEqual(r['jev_calls'],0)
if __name__=='__main__':unittest.main()
