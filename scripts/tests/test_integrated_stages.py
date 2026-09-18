import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import outbound_fixtures as OF
SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import routing_fixtures as F
import executor_runtime as E
from stage_contracts import ContractError
class Judge:
    def __init__(self,p=.01):self.p=p;self.calls=0
    def evaluate(self,state,questions):
        self.calls+=1
        return {'model':'jev-1.13.0','answers':{k:{'type':'noul','noul':self.p} for k in questions},'usage':{'input_tokens':1,'output_tokens':1}}
class IntegratedTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
        self.s=F.stage(self.root);self.reg=F.registry();self.adapter=F.attach_adapter(self.root,self.reg);self.run=self.root/'run'
    def start(self):E.initialize(self.s,self.reg,self.run,evidence_mode='synthetic')
    def advance(self,**kwargs):
        policies={}
        if self.s['features']['context_rerank']:
            req=OF.context_requests(self.s['objective'],self.s['context'])
            policies['context_reranking']=OF.admission(req,'context_reranking')
        if self.s['features']['semantic_cascade']:
            try:
                req=OF.semantic_requests(self.s['semantic_flags'],
                    {'source':(self.run/'inputs/0.bin').read_text()},
                    {'sample.py':'def add(a, b):\n    return a + b\n'})
            except UnicodeError:req=[]
            policies['semantic_cascade']=OF.admission(req,'semantic_cascade')
        with patch('outbound_admission.canonical_redact',OF.redact):
            return E.advance(self.run,fixed_route='fixture_worker',admissions=policies,**kwargs)
    def semantics(self):
        self.s['features']['semantic_cascade']=True
        self.s['semantic_flags']=[{'id':'unsupported_value','source_id':'source','output_path':'sample.py'}]
    def retry_adapter(self):
        p=Path(self.adapter['argv'][-1]);p.write_text(F.ADAPTER.replace('target.write_text("def add(a, b):\\n    return a + b\\n")',
          'target.write_text("def add(a, b):\\n    return a " + ("-" if packet.get("execution",{}).get("attempt",1)==1 else "+") + " b\\n")'))
        self.adapter['pins'][str(p)]=F.sha(p)
    def test_context_reaches_packet_with_mandatory(self):
        p=self.root/'context.txt';p.write_text('Required signature detail.\n')
        self.s['context']={'candidates':[{'id':'required','source_path':str(p),'source_sha256':F.sha(p),'start_line':1,'end_line':1,'mandatory':True}],
                           'mandatory_ids':['required'],'top_k':1}
        self.s['features']['context_rerank']=True;self.start();r=self.advance(providers={'context_reranking':Judge(.01)})
        self.assertEqual(r['status'],'ready_for_parent_review')
        packet=json.loads((self.run/'attempts/1/worker-packet.json').read_text())
        self.assertEqual(packet['selected_context'][0]['text'],'Required signature detail.\n')
        self.assertEqual(packet['workflow']['process_owner']['name'],'shaw')
    def test_deterministic_failure_skips_semantic_provider(self):
        F.attach_adapter(self.root,self.reg,'bad_result');self.semantics();self.start();j=Judge()
        r=self.advance(providers={'semantic_cascade':j});self.assertEqual(j.calls,0)
        self.assertEqual(r['last_result']['semantic']['status'],'blocked_deterministic')
    def test_semantic_flag_vetoes_passing_checks(self):
        self.semantics();self.start();r=self.advance(providers={'semantic_cascade':Judge(.99)})
        self.assertEqual(r['status'],'needs_review');self.assertTrue(r['last_result']['checks'][0]['passed'])
        self.assertFalse(r['parent_accepted'])
    def test_mutation_during_semantics_blocks(self):
        self.semantics();self.start();root=Path(self.s['workspace'])
        class Mutate(Judge):
            def evaluate(self,s,q):(root/'sample.py').write_text('changed');return super().evaluate(s,q)
        r=self.advance(providers={'semantic_cascade':Mutate()})
        self.assertEqual(r['status'],'needs_review');self.assertIn('__post_check_source_drift__',r['last_result']['scope_violations'])
    def test_binary_source_fails_closed_not_exception(self):
        self.semantics();p=Path(self.s['workspace'])/'sample.py';p.write_bytes(b'\xff')
        self.s['inputs'][0]['sha256']=F.sha(p);self.start();r=self.advance(providers={'semantic_cascade':Judge()})
        self.assertEqual(r['status'],'needs_review');self.assertEqual(r['last_result']['semantic']['status'],'needs_review')
    def test_transition_disabled_by_default(self):
        self.assertTrue(hasattr(E,'transition'),'runtime transition missing')
        self.start();self.advance()
        with self.assertRaises(ContractError):E.transition(self.run,proposed='complete')
    def test_one_actual_correction_and_no_third_worker(self):
        self.assertTrue(hasattr(E,'transition'),'runtime transition missing')
        self.retry_adapter();self.s['features']['stage_transition']=True;self.start();first=self.advance()
        self.assertEqual(first['status'],'needs_review')
        E.transition(self.run,proposed='one_bounded_correction',parent_authorized_correction=True,triage_category='implementation_defect')
        second=self.advance();self.assertEqual(second['attempts'],2);self.assertEqual(second['status'],'ready_for_parent_review')
        p=json.loads((self.run/'attempts/2/worker-packet.json').read_text())
        self.assertEqual(p['execution']['attempt'],2);self.assertEqual(p['execution']['previous_evidence_hash'],first['result_hash'])
        self.assertEqual(p['execution']['baseline_tree_hash'],first['last_result']['final_tree_hash'])
        self.assertEqual(p['execution']['current_input_hashes']['source'],self.s['inputs'][0]['sha256'])
        with self.assertRaises(ContractError):E.transition(self.run,proposed='one_bounded_correction',parent_authorized_correction=True,triage_category='implementation_defect')
        self.assertEqual(self.advance()['attempts'],2)
    def test_no_external_edit_between_correction_and_execution(self):
        self.assertTrue(hasattr(E,'transition'),'runtime transition missing')
        self.retry_adapter();self.s['features']['stage_transition']=True;self.start();self.advance()
        E.transition(self.run,proposed='one_bounded_correction',parent_authorized_correction=True,triage_category='implementation_defect')
        (Path(self.s['workspace'])/'sample.py').write_text('unapproved')
        with self.assertRaises(ContractError):self.advance()
    def test_correction_requires_parent_authorization(self):
        self.assertTrue(hasattr(E,'transition'),'runtime transition missing')
        self.retry_adapter();self.s['features']['stage_transition']=True;self.start();self.advance()
        with self.assertRaises(ContractError):E.transition(self.run,proposed='one_bounded_correction',triage_category='implementation_defect')
    def test_complete_transition_is_not_acceptance(self):
        self.assertTrue(hasattr(E,'transition'),'runtime transition missing')
        self.s['features']['stage_transition']=True;self.start();self.advance();r=E.transition(self.run,proposed='complete')
        self.assertEqual(r['status'],'ready_for_parent_review');self.assertFalse(r['parent_accepted'])
    def test_high_risk_transition_cannot_reopen_worker(self):
        self.assertTrue(hasattr(E,'transition'),'runtime transition missing')
        self.s['features']['stage_transition']=True;self.s['risk']['money']=True;self.start();E.advance(self.run)
        with self.assertRaises(ContractError):E.transition(self.run,proposed='one_bounded_correction',parent_authorized_correction=True,triage_category='implementation_defect')
    def test_repeated_transition_without_new_evidence_blocks(self):
        self.s['features']['stage_transition']=True;self.start();self.advance();E.transition(self.run,proposed='complete')
        with self.assertRaises(ContractError):E.transition(self.run,proposed='complete')
    def test_context_change_during_semantics_blocks(self):
        p=self.root/'context.txt';p.write_text('Required context.\n')
        self.s['context']={'candidates':[{'id':'required','source_path':str(p),'source_sha256':F.sha(p),'start_line':1,'end_line':1,'mandatory':True}],
                          'mandatory_ids':['required'],'top_k':1}
        self.semantics();self.start()
        class Mutate(Judge):
            def evaluate(self,s,q):p.write_text('new source');return super().evaluate(s,q)
        r=self.advance(providers={'semantic_cascade':Mutate()})
        self.assertEqual(r['status'],'needs_review')
if __name__=='__main__':unittest.main()
