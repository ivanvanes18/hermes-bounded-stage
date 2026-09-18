"""I-2 runtime wiring and evidence freshness, using real staged controller paths."""
import hashlib
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import executor_runtime as E
import routing_fixtures as F
import outbound_fixtures as OF
from test_correction_outbound import Recorder,CANARY

class RuntimeOutboundTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.s=F.stage(self.root);self.reg=F.registry();F.attach_adapter(self.root,self.reg);self.run=self.root/'run'
        self.context=self.root/'context.txt';self.context.write_text('Addition '+OF.REDACT_MARKER+'\n')
        self.s['context']={'candidates':[{'id':'ctx','source_path':str(self.context),'source_sha256':F.sha(self.context),
            'start_line':1,'end_line':1,'mandatory':True}],'mandatory_ids':['ctx'],'top_k':1}
        self.s['semantic_flags']=[{'id':'unsupported_value','source_id':'source','output_path':'sample.py'}]
        self.expected_output='def add(a, b):\n    return a + b\n'
        p=patch('outbound_admission.canonical_redact',OF.redact);p.start();self.addCleanup(p.stop)
    def setup_feature(self,purpose):
        self.s['features']['context_rerank' if purpose=='context_reranking' else 'semantic_cascade']=True
        if purpose=='semantic_cascade':self.s['context']={'candidates':[],'mandatory_ids':[],'top_k':1}
    def policies(self,purpose):
        if purpose=='context_reranking':req=OF.context_requests(self.s['objective'],self.s['context'])
        else:req=OF.semantic_requests(self.s['semantic_flags'],
            {'source':(Path(self.s['workspace'])/'sample.py').read_text()}, {'sample.py':self.expected_output})
        return {purpose:OF.admission(req,purpose)}
    def advance(self,purpose,policies):
        j=Recorder();kw={'providers':{purpose:j},'fixed_route':'fixture_worker'}
        if 'admissions' in inspect.signature(E.advance).parameters:kw['admissions']=policies
        r=E.advance(self.run,**kw);return j,r
    def test_exact_policy_context_runtime(self):
        purpose='context_reranking';self.setup_feature(purpose);policies=self.policies(purpose)
        E.initialize(self.s,self.reg,self.run,evidence_mode='synthetic');j,r=self.advance(purpose,policies)
        self.assertEqual(len(j.payloads),1,'runtime did not wire parent admission to context')
        self.assertEqual(r['status'],'ready_for_parent_review')
        receipt=(self.run/'attempts/1/context-receipt.json').read_text()
        self.assertNotIn(OF.REDACT_MARKER,receipt)
        packet=json.loads((self.run/'attempts/1/worker-packet.json').read_text())
        self.assertEqual(packet['selected_context'][0]['text'],self.context.read_text())
    def test_exact_policy_semantic_runtime(self):
        purpose='semantic_cascade';self.setup_feature(purpose);policies=self.policies(purpose)
        E.initialize(self.s,self.reg,self.run,evidence_mode='synthetic');j,r=self.advance(purpose,policies)
        self.assertEqual(len(j.payloads),1,'runtime did not wire parent admission to semantic cascade')
        self.assertEqual(r['status'],'ready_for_parent_review')
        self.assertNotIn('return a + b',json.dumps(r['last_result']['semantic']))
    def test_missing_policy_both_runtime_paths_zero_calls(self):
        # Each subcase has an independently sealed stage/run.
        for index,purpose in enumerate(('context_reranking','semantic_cascade')):
            self.s['features'].update(context_rerank=False,semantic_cascade=False);self.setup_feature(purpose)
            self.run=self.root/('run-'+str(index))
            E.initialize(self.s,self.reg,self.run,evidence_mode='synthetic');j,r=self.advance(purpose,{})
            self.assertEqual(j.payloads,[]);self.assertEqual(r['status'],'needs_review')
    def test_semantic_sealed_source_drift_before_send_zero_calls(self):
        purpose='semantic_cascade';self.setup_feature(purpose);policies=self.policies(purpose)
        E.initialize(self.s,self.reg,self.run,evidence_mode='synthetic')
        def mutate(text):
            (self.run/'inputs/0.bin').write_text('changed sealed source')
            return OF.redact(text)
        with patch('outbound_admission.canonical_redact',mutate):j,r=self.advance(purpose,policies)
        self.assertEqual(j.payloads,[]);self.assertEqual(r['status'],'needs_review')
    def test_semantic_output_drift_before_send_zero_calls(self):
        purpose='semantic_cascade';self.setup_feature(purpose);policies=self.policies(purpose)
        E.initialize(self.s,self.reg,self.run,evidence_mode='synthetic')
        def mutate(text):
            (Path(self.s['workspace'])/'sample.py').write_text('changed checked output')
            return OF.redact(text)
        with patch('outbound_admission.canonical_redact',mutate):j,r=self.advance(purpose,policies)
        self.assertEqual(j.payloads,[]);self.assertEqual(r['status'],'needs_review')

if __name__=='__main__':unittest.main()
