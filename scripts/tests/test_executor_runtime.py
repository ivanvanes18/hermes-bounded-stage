import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import routing_fixtures as F
import outbound_fixtures as OF
try:
    import executor_runtime as E
    import harness_adapter as H
    import executor_routes as R
    import stage_contracts as C
except ImportError:E=H=None

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(E,'executor runtime missing')
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
        self.s=F.stage(self.root);self.reg=F.registry();self.adapter=F.attach_adapter(self.root,self.reg)
        self.run=self.root/'run'
    def init(self,**kw):return E.initialize(self.s,self.reg,self.run,evidence_mode='synthetic',**kw)
    def advance(self,**kw):return E.advance(self.run,fixed_route='fixture_worker',**kw)
    def variant(self,v):self.adapter=F.attach_adapter(self.root,self.reg,v)
    def test_real_probe_identity(self):
        p=H.readiness(self.s,self.reg,evidence_mode='synthetic')
        self.assertTrue(p['fixture_worker']['ready']);self.assertEqual(p['fixture_worker']['model'],'fixture-model-1')
    def test_probe_mismatch_unready(self):
        self.variant('wrong_model');p=H.readiness(self.s,self.reg,evidence_mode='synthetic')
        self.assertFalse(p['fixture_worker']['ready'])
    def test_synthetic_not_live(self):
        with self.assertRaises(C.ContractError):E.initialize(self.s,self.reg,self.run,evidence_mode='live')
    def test_create_only(self):
        self.init()
        with self.assertRaises(C.ContractError):self.init()
    def test_run_not_inside_worker(self):
        with self.assertRaises(C.ContractError):E.initialize(self.s,self.reg,Path(self.s['workspace'])/'control',evidence_mode='synthetic')
    def test_full_fixed_route_actual_write_and_checks(self):
        self.init();r=self.advance()
        self.assertEqual(r['status'],'ready_for_parent_review');self.assertFalse(r['parent_accepted'])
        self.assertEqual(r['attempts'],1);self.assertTrue(all(x['passed'] for x in r['last_result']['checks']))
        self.assertIn('return a + b',(Path(self.s['workspace'])/'sample.py').read_text())
        self.assertEqual(r['last_result']['proof']['model'],'fixture-model-1')
    def test_jev_route_uses_real_worker_path(self):
        self.init()
        class J:
            def evaluate(self,state,questions):return F.routing_reply({'questions':questions})
        readiness=H.readiness(self.s,self.reg,evidence_mode='synthetic')
        request=R.request(self.s,self.reg,readiness)
        admission=OF.admission([(request['state'],request['questions'])],'executor_routing')
        with patch('outbound_admission.canonical_redact',OF.redact):
            r=E.advance(self.run,jev=J(),admissions={'executor_routing':admission})
        self.assertEqual(r['status'],'ready_for_parent_review');self.assertEqual(r['jev_calls'],1)
        receipt=json.loads((self.run/'attempts/1/route-receipt.json').read_text())
        self.assertEqual(receipt['source'],'jev')
    def test_second_advance_no_second_worker(self):
        self.init();self.advance()
        with patch.object(H,'execute',side_effect=AssertionError('must not repeat')):
            r=self.advance();self.assertEqual(r['attempts'],1)
    def test_bad_worker_result_checks_fail(self):
        self.variant('bad_result');self.init();r=self.advance()
        self.assertEqual(r['status'],'needs_review');self.assertFalse(r['last_result']['checks'][0]['passed'])
    def test_scope_violation_even_checks_pass(self):
        self.variant('scope_violation');self.init();r=self.advance()
        self.assertEqual(r['status'],'needs_review');self.assertIn('unrelated.txt',r['last_result']['scope_violations'])
    def test_wrong_runtime_model_refused(self):
        self.variant('wrong_runtime_model');self.init();r=self.advance()
        self.assertEqual(r['status'],'needs_review');self.assertEqual(r['reason'],'runtime_proof_mismatch')
    def test_wrong_nonce_refused(self):
        self.variant('wrong_nonce');self.init();r=self.advance()
        self.assertEqual(r['status'],'needs_review')
    def test_permissions_expansion_refused(self):
        self.variant('wrong_permissions');self.init();r=self.advance()
        self.assertEqual(r['status'],'needs_review')
    def test_timeout_is_not_success(self):
        self.variant('timeout');self.init();r=self.advance()
        self.assertEqual(r['status'],'needs_review');self.assertEqual(r['attempts'],1)
    def test_stdout_budget(self):
        self.variant('large_stdout');self.init();r=self.advance()
        self.assertEqual(r['status'],'needs_review')
    def test_registry_tamper(self):
        self.init();p=self.run/'registry.json';p.write_text('{}')
        with self.assertRaises(C.ContractError):self.advance()
    def test_plan_tamper(self):
        self.init();p=self.run/'stage.json';x=json.loads(p.read_text());x['allowed_paths'].append('unrelated.txt');p.write_text(json.dumps(x))
        with self.assertRaises(C.ContractError):self.advance()
    def test_input_drift_before_first_worker(self):
        self.init();(Path(self.s['workspace'])/'sample.py').write_text('changed')
        with self.assertRaises(C.ContractError):self.advance()
    def test_adapter_drift(self):
        self.init();Path(self.adapter['argv'][-1]).write_text('print("changed")')
        with self.assertRaises(C.ContractError):self.advance()
    def test_methodology_drift(self):
        self.init();Path(self.s['methodology']['process_owner']['source_path']).write_text('changed')
        with self.assertRaises(C.ContractError):self.advance()
    def test_parent_acceptance_requires_matching_evidence(self):
        self.init();r=self.advance()
        with self.assertRaises(C.ContractError):E.accept(self.run,{'decision':'accept','evidence_hash':'0'*64,'reviewer':'fixture-parent','evidence_kind':'synthetic'})
        a={'decision':'accept','evidence_hash':r['result_hash'],'reviewer':'fixture-parent','evidence_kind':'synthetic'}
        r=E.accept(self.run,a);self.assertTrue(r['parent_accepted']);self.assertEqual(r['status'],'parent_accepted')
    def test_changed_result_blocks_parent_acceptance(self):
        self.init();r=self.advance();(Path(self.s['workspace'])/'sample.py').write_text('different')
        with self.assertRaises(C.ContractError):E.accept(self.run,{'decision':'accept','evidence_hash':r['result_hash'],'reviewer':'fixture-parent','evidence_kind':'synthetic'})
    def test_worker_self_approval_not_used(self):
        self.init();r=self.advance();self.assertFalse(r['parent_accepted'])
    def test_interrupted_worker_cannot_implicitly_retry(self):
        self.init();p=self.run/'state.json';x=json.loads(p.read_text());x.update(status='worker_running',attempts=1);p.write_text(json.dumps(x))
        with patch.object(H,'execute') as run:
            r=self.advance();run.assert_not_called();self.assertEqual(r['status'],'needs_review')
    def test_no_provider_when_hard_owner(self):
        self.s['risk']['production']=True;self.init()
        class J:
            def evaluate(*args):raise AssertionError('no provider')
        r=E.advance(self.run,jev=J());self.assertEqual(r['status'],'needs_review');self.assertEqual(r['reason'],'hard_owner_boundary')
    def test_one_writer_lock(self):
        import fcntl,os
        self.init();fd=os.open(Path(self.s['workspace'])/'.staged-routing-workspace.json',os.O_RDONLY)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(C.ContractError):self.advance()
        finally:os.close(fd)
if __name__=='__main__':unittest.main()
