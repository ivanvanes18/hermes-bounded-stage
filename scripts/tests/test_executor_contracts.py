import copy
from pathlib import Path
import sys
import tempfile
import unittest
SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import routing_fixtures as F
try:
    import stage_contracts as C
    import executor_routes as R
except ImportError:C=R=None

class ExecutorContracts(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(C,'stage routing contracts missing')
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
        self.s=F.stage(self.root);self.reg=F.registry()
    def test_valid_stage_and_registry(self):
        C.validate_stage(self.s);R.validate_registry(self.reg,require_adapters=False)
    def test_closed_stage(self):
        self.s['conversation']='private'
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
    def test_multiple_process_owners(self):
        self.s['methodology']['process_owner']=[self.s['methodology']['process_owner']]*2
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
    def test_missing_process_owner(self):
        del self.s['methodology']['process_owner']
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
    def test_methodology_source_hash_mismatch(self):
        Path(self.s['methodology']['process_owner']['source_path']).write_text('changed')
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
    def test_methodology_version_mismatch(self):
        self.s['methodology']['process_owner']['version']='different'
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
    def test_rule_source_binding(self):
        self.s['methodology']['domain_rules'][0]['source_sha256']='0'*64
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
    def test_scope_path_escape(self):
        self.s['allowed_paths']=['../outside']
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
    def test_source_symlink_refused(self):
        w=Path(self.s['workspace']);(w/'sample.py').unlink();(w/'sample.py').symlink_to('/etc/hosts')
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
    def test_readonly_has_no_write_scope(self):
        self.s['mode']='read_only'
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
    def test_check_not_worker_writable(self):
        p=Path(self.s['workspace'])/'sample.py';self.s['checks'][0]['argv'][1]=str(p)
        self.s['checks'][0]['pins'][str(p)]=F.sha(p)
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
    def test_required_output_must_be_allowed_or_input(self):
        self.s['output_contract']['required_paths']=['unrelated.txt']
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
    def test_unknown_route(self):
        self.s['executor_candidates']=['unknown']
        with self.assertRaises(C.ContractError):R.available(self.s,self.reg,{})
    def test_disabled_not_available(self):
        self.s['executor_candidates']=['claude_opus_high','owner']
        self.assertEqual(R.available(self.s,self.reg,{}),['owner'])
    def test_duplicate_route(self):
        self.reg['routes'].append(copy.deepcopy(self.reg['routes'][0]))
        with self.assertRaises(C.ContractError):R.validate_registry(self.reg,require_adapters=False)
    def test_unknown_status(self):
        self.reg['routes'][-1]['status']='trusted-by-model'
        with self.assertRaises(C.ContractError):R.validate_registry(self.reg,require_adapters=False)
    def test_latest_alias_never_approved(self):
        self.reg['routes'][-1]['model']='jev-latest'
        with self.assertRaises(C.ContractError):R.validate_registry(self.reg,require_adapters=False)
    def test_luna_write_needs_benchmark(self):
        r=self.reg['routes'][-1];r['route_id']='luna_low';self.reg['routes']=[x for x in self.reg['routes'] if x['route_id']!='luna_low']+[r]
        r['write_benchmark_sha256']=None
        with self.assertRaises(C.ContractError):R.validate_registry(self.reg,require_adapters=False)
    def test_readiness_model_mismatch(self):
        p=F.ready(self.reg,C.digest(self.s));p['fixture_worker']['model']='other'
        self.assertEqual(R.available(self.s,self.reg,p),['owner'])
    def test_readiness_binds_stage(self):
        p=F.ready(self.reg,'0'*64)
        self.assertEqual(R.available(self.s,self.reg,p),['owner'])
    def test_expired_readiness(self):
        p=F.ready(self.reg,C.digest(self.s));p['fixture_worker']['expires_at']=0
        self.assertEqual(R.available(self.s,self.reg,p),['owner'])
    def test_mode_incompatible(self):
        p=F.ready(self.reg,C.digest(self.s));p['fixture_worker']['supported_modes']=['read_only']
        self.assertEqual(R.available(self.s,self.reg,p),['owner'])
    def test_full_route_decision(self):
        p=F.ready(self.reg,C.digest(self.s));q=R.request(self.s,self.reg,p)
        receipt=R.decide(self.s,self.reg,p,F.routing_reply(q))
        self.assertEqual(receipt['selected_route'],'fixture_worker');self.assertIn('route',receipt['answers'])
        self.assertEqual(receipt['methodology_hash'],C.digest(self.s['methodology']))
    def test_hard_owner_override(self):
        self.s['risk']['money']=True;p=F.ready(self.reg,C.digest(self.s))
        r=R.decide(self.s,self.reg,p,None)
        self.assertEqual(r['selected_route'],'owner');self.assertIn('hard_owner_boundary',r['hard_overrides'])
    def test_low_confidence_not_cheap_fallback(self):
        p=F.ready(self.reg,C.digest(self.s));q=R.request(self.s,self.reg,p);a=F.routing_reply(q);a['answers']['route']['confidence']=.4
        self.assertEqual(R.decide(self.s,self.reg,p,a)['selected_route'],'owner')
    def test_risk_noul_true_blocks(self):
        p=F.ready(self.reg,C.digest(self.s));q=R.request(self.s,self.reg,p)
        self.assertEqual(R.decide(self.s,self.reg,p,F.routing_reply(q,risk=.95))['selected_route'],'owner')
    def test_malformed_timeout_owner(self):
        p=F.ready(self.reg,C.digest(self.s))
        for bad in (None,{}, {'model':'bad'}):self.assertEqual(R.decide(self.s,self.reg,p,bad)['selected_route'],'owner')
    def test_worker_packet_is_compact_and_bound(self):
        p=F.ready(self.reg,C.digest(self.s));q=R.request(self.s,self.reg,p);r=R.decide(self.s,self.reg,p,F.routing_reply(q))
        packet=C.worker_packet(self.s,r,'a'*32)
        self.assertEqual(packet['stage_hash'],C.digest(self.s));self.assertEqual(packet['workflow']['process_owner']['name'],'shaw')
        self.assertNotIn('source_path',str(packet['workflow']));self.assertNotIn('---\nname:',str(packet))
        self.assertNotIn('registry',packet);self.assertNotIn('conversation',packet)
    def test_receipt_mutation_rejected(self):
        p=F.ready(self.reg,C.digest(self.s));q=R.request(self.s,self.reg,p);r=R.decide(self.s,self.reg,p,F.routing_reply(q))
        r['methodology_hash']='0'*64
        with self.assertRaises(C.ContractError):C.worker_packet(self.s,r,'a'*32)
    def test_packet_raw_history_rejected(self):
        self.s['methodology']['history']='raw'
        with self.assertRaises(C.ContractError):C.validate_stage(self.s)
if __name__=='__main__':unittest.main()
