from pathlib import Path
import sys
import unittest
from outbound_fixtures import cascade
SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
try:import semantic_cascade as C
except ImportError:C=None
class Judge:
    def __init__(self,probs):self.probs=probs;self.calls=0
    def evaluate(self,s,q):
        self.calls+=1
        return {'model':'jev-1.13.0','answers':{k:{'type':'noul','noul':self.probs.get(k,.01)} for k in q},'usage':{'input_tokens':5,'output_tokens':3}}
class CascadeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(C,'semantic cascade missing')
        self.flags=[{'id':'unsupported_value','source_id':'input','output_path':'out'},
                    {'id':'omitted_required_fact','source_id':'input','output_path':'out'}]
    def test_deterministic_failure_no_provider(self):
        j=Judge({});r=cascade(False,self.flags,{}, {},j)
        self.assertEqual(j.calls,0);self.assertEqual(r['status'],'blocked_deterministic')
    def test_all_clear(self):
        r=cascade(True,self.flags,{'input':'source'},{'out':'output'},Judge({}))
        self.assertEqual(r['status'],'clear')
    def test_max_gate_not_average(self):
        r=cascade(True,self.flags,{'input':'source'},{'out':'output'},Judge({'unsupported_value':.99,'omitted_required_fact':0}))
        self.assertEqual(r['status'],'needs_review');self.assertEqual(r['exceptions'][0]['flag_id'],'unsupported_value')
    def test_boundary_flags_not_rounded_down(self):
        r=cascade(True,self.flags,{'input':'source'},{'out':'output'},Judge({'unsupported_value':.10}))
        self.assertEqual(r['status'],'needs_review')
    def test_source_pointer_and_hash(self):
        r=cascade(True,self.flags,{'input':'source'},{'out':'output'},Judge({'unsupported_value':.8}))
        e=r['exceptions'][0];self.assertEqual(e['source_id'],'input');self.assertEqual(len(e['source_sha256']),64)
    def test_missing_source(self):
        j=Judge({});r=cascade(True,self.flags,{}, {'out':'output'},j)
        self.assertEqual(r['status'],'needs_review');self.assertEqual(j.calls,0)
    def test_empty_flag_list_not_semantic_pass(self):
        self.assertEqual(cascade(True,[],{}, {},Judge({}))['status'],'needs_review')
    def test_no_provider_not_clear(self):
        self.assertEqual(cascade(True,self.flags,{'input':'source'},{'out':'output'},None)['status'],'needs_review')
    def test_duplicate_flags_block(self):
        self.assertEqual(cascade(True,self.flags*2,{'input':'source'},{'out':'output'},Judge({}))['status'],'needs_review')
    def test_no_truncation_of_large_evidence(self):
        self.assertEqual(cascade(True,self.flags,{'input':'x'*20000},{'out':'output'},Judge({}))['status'],'needs_review')
    def test_triage_unknown_is_not_fix(self):
        r=C.triage({'deterministic_passed':False,'expected_red':False,'environment_failure':False,'missing_dependency':False,
                    'missing_evidence':False,'scope_mismatch':False,'stale_plan':False},None)
        self.assertEqual(r['category'],'unknown');self.assertFalse(r['automatic_fix'])
    def test_triage_expected_red(self):
        r=C.triage({'deterministic_passed':False,'expected_red':True,'environment_failure':False,'missing_dependency':False,
                    'missing_evidence':False,'scope_mismatch':False,'stale_plan':False},None)
        self.assertEqual(r['category'],'expected_red')
    def test_triage_full_categories(self):
        self.assertEqual(set(C.TRIAGE_CATEGORIES),{'implementation_defect','test_defect','environment_failure','missing_dependency',
                         'missing_evidence','scope_mismatch','stale_plan','expected_red','unknown'})
if __name__=='__main__':unittest.main()
