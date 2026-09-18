"""I-2 causal probes use synthetic markers, never credentials or client documents."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import context_rerank as R
import semantic_cascade as S

CANARY='SECRET_CANARY_7429'  # synthetic marker, not a credential

class Recorder:
    def __init__(self):self.payloads=[]
    def evaluate(self,data,questions):
        self.payloads.append(copy.deepcopy({'state':data,'questions':questions}))
        return {'model':'jev-1.13.0','answers':{k:{'type':'noul','noul':.01} for k in questions},
                'usage':{'input_tokens':0,'output_tokens':0}}

class OutboundCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.source=self.root/'evidence.txt';self.source.write_text(CANARY+'\n')
        self.context={'candidates':[{'id':'evidence','source_path':str(self.source),
            'source_sha256':hashlib.sha256(self.source.read_bytes()).hexdigest(),'start_line':1,'end_line':1,'mandatory':True}],
            'mandatory_ids':['evidence'],'top_k':1}
        self.flags=[{'id':'unsupported_value','source_id':'input','output_path':'out'}]
    def test_context_secret_without_admission_zero_provider_calls(self):
        j=Recorder();result=R.select_context(CANARY,self.context,j,max_chars=4000)
        self.assertEqual(j.payloads,[],'context raw evidence reached provider without admission')
        self.assertNotIn(CANARY,json.dumps(result),'context receipt stores raw evidence')
    def test_semantic_secret_without_admission_zero_provider_calls(self):
        j=Recorder();result=S.cascade(True,self.flags,{'input':CANARY},{'out':CANARY},j)
        self.assertEqual(j.payloads,[],'semantic raw evidence reached provider without admission')
        self.assertNotIn(CANARY,json.dumps(result),'semantic receipt stores raw evidence')
    def test_context_receipt_never_persists_raw_source_even_local(self):
        result=R.select_context('local',self.context,None,max_chars=4000)
        self.assertNotIn(CANARY,json.dumps(result),'local context receipt stores raw source')


# New API tests follow the already-reproduced raw-byte causal probes above.
from unittest.mock import patch
import outbound_fixtures as F
try:import outbound_admission as O
except ImportError:O=None
REAL_CANONICAL_REDACT=O.canonical_redact if O is not None else None

class OutboundAdmissionMatrix(OutboundCorrectionTests):
    def setUp(self):
        super().setUp()
        self.assertIsNotNone(O,'common fail-closed outbound admission is absent')
        self.source.write_text('Public fixture '+F.REDACT_MARKER+'\n')
        self.context['candidates'][0]['source_sha256']=hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.sources={'input':'Public source '+F.REDACT_MARKER}
        self.outputs={'out':'Public output '+F.REDACT_MARKER}
        self.objective='Objective '+F.REDACT_MARKER
        p=patch.object(O,'canonical_redact',F.redact);p.start();self.addCleanup(p.stop)
    def requests(self,purpose):
        return (F.context_requests(self.objective,self.context) if purpose=='context_reranking' else
                F.semantic_requests(self.flags,self.sources,self.outputs))
    def call(self,purpose,policy,*,classification='synthetic',guard=None):
        j=Recorder()
        if purpose=='context_reranking':
            r=R.select_context(self.objective,self.context,j,max_chars=4000,admission=policy,classification=classification)
        else:r=S.cascade(True,self.flags,self.sources,self.outputs,j,admission=policy,classification=classification,source_guard=guard)
        return j,r
    def test_exact_grant_redacts_both_paths(self):
        for purpose in ('context_reranking','semantic_cascade'):
            with self.subTest(purpose=purpose):
                j,r=self.call(purpose,F.admission(self.requests(purpose),purpose))
                self.assertEqual(len(j.payloads),1)
                self.assertNotIn(F.REDACT_MARKER,json.dumps(j.payloads))
                self.assertIn('[REDACTED]',json.dumps(j.payloads))
                self.assertNotIn('Public source',json.dumps(r));self.assertNotIn('Public output',json.dumps(r))
                self.assertNotIn('Public fixture',json.dumps(r))
    def test_private_contract_act_secret_classifications_zero_calls(self):
        for purpose in ('context_reranking','semantic_cascade'):
            for classification in ('private','contract','act','secret','unknown'):
                with self.subTest(purpose=purpose,classification=classification):
                    # Even a parent synthetic grant and a redactor that removes
                    # data cannot turn a restricted document into public data.
                    policy=F.admission(self.requests(purpose),purpose,classifier=lambda raw,c=classification:c)
                    j,r=self.call(purpose,policy)
                    self.assertEqual(j.payloads,[])
                    self.assertEqual(r['status'],'needs_review')
    def test_parent_classification_cannot_be_upgraded(self):
        for purpose in ('context_reranking','semantic_cascade'):
            j,r=self.call(purpose,F.admission(self.requests(purpose),purpose),classification='private')
            self.assertEqual(j.payloads,[])
    def test_canary_denied_even_if_redactor_would_remove_it(self):
        self.objective=CANARY;self.sources['input']=CANARY
        with patch.object(O,'canonical_redact',lambda text:text.replace(CANARY,'[REDACTED]')):
            for purpose in ('context_reranking','semantic_cascade'):
                j,r=self.call(purpose,F.admission(self.requests(purpose),purpose))
                self.assertEqual(j.payloads,[]);self.assertNotIn(CANARY,json.dumps(r))
    def test_contract_outside_selected_fragment_is_classified(self):
        self.source.write_text('Public fragment\nCONTRACT_CLASSIFICATION_FIXTURE\n')
        self.context['candidates'][0]['source_sha256']=hashlib.sha256(self.source.read_bytes()).hexdigest()
        seen=[]
        def classify(raw):
            seen.append(raw)
            return 'contract' if 'CONTRACT_CLASSIFICATION_FIXTURE' in json.dumps(raw) else 'synthetic'
        p=F.admission(self.requests('context_reranking'),'context_reranking',classifier=classify)
        j,r=self.call('context_reranking',p)
        self.assertTrue(seen);self.assertEqual(j.payloads,[])
    def test_missing_classifier_zero_calls(self):
        for purpose in ('context_reranking','semantic_cascade'):
            j,r=self.call(purpose,F.admission(self.requests(purpose),purpose,classifier=None))
            self.assertEqual(j.payloads,[])
    def test_missing_redactor_zero_calls(self):
        with patch.object(O,'canonical_redact',side_effect=ValueError('not installed')):
            for purpose in ('context_reranking','semantic_cascade'):
                j,r=self.call(purpose,F.admission(self.requests(purpose),purpose))
                self.assertEqual(j.payloads,[])
    def test_grant_missing_mismatched_expired_zero_calls(self):
        import time
        for purpose in ('context_reranking','semantic_cascade'):
            base=[F.grant(s,q,purpose) for s,q in self.requests(purpose)]
            variants={'missing':[], 'hash':{**base[0],'payload_sha256':'0'*64},
                'purpose':{**base[0],'purpose':'executor_routing'},
                'classification':{**base[0],'classification':'public-redacted'},
                'expired':{**base[0],'expires_at':time.time()-1},
                'nonfinite':{**base[0],'expires_at':float('nan')},
                'too_long':{**base[0],'expires_at':time.time()+7200},
                'worker':{**base[0],'approved_by':'worker'}}
            for name,g in variants.items():
                with self.subTest(purpose=purpose,case=name):
                    grants=g if isinstance(g,list) else [g]
                    j,r=self.call(purpose,F.admission(self.requests(purpose),purpose,grants=grants))
                    self.assertEqual(j.payloads,[]);self.assertEqual(r['status'],'needs_review')
    def test_changed_payload_after_grant_zero_calls(self):
        policies={p:F.admission(self.requests(p),p) for p in ('context_reranking','semantic_cascade')}
        self.objective+=' CHANGED';self.outputs['out']+=' CHANGED'
        for purpose,p in policies.items():
            j,r=self.call(purpose,p);self.assertEqual(j.payloads,[])
    def test_source_drift_during_redaction_zero_calls(self):
        requests=self.requests('context_reranking');p=F.admission(requests,'context_reranking')
        def mutate(text):self.source.write_text('changed');return F.redact(text)
        with patch.object(O,'canonical_redact',mutate):j,r=self.call('context_reranking',p)
        self.assertEqual(j.payloads,[]);self.assertEqual(r['status'],'needs_review')
    def test_semantic_source_guard_drift_zero_calls(self):
        from schema_validation import ContractError
        p=F.admission(self.requests('semantic_cascade'),'semantic_cascade')
        def drift():raise ContractError('source_changed')
        j,r=self.call('semantic_cascade',p,guard=drift)
        self.assertEqual(j.payloads,[]);self.assertEqual(r['status'],'needs_review')
    def test_all_batch_grants_preflight_before_any_send(self):
        c=copy.deepcopy(self.context['candidates'][0]);c['id']='other';self.context['candidates'].append(c)
        requests=F.context_requests(self.objective,self.context,1)
        p=F.admission(requests,'context_reranking',grants=[F.grant(*requests[0],'context_reranking')])
        j=Recorder();r=R.select_context(self.objective,self.context,j,max_chars=4000,batch_size=1,
            admission=p,classification='synthetic')
        self.assertEqual(j.payloads,[]);self.assertEqual(r['status'],'needs_review')
    def test_public_redacted_requires_its_own_exact_grant(self):
        for purpose in ('context_reranking','semantic_cascade'):
            p=F.admission(self.requests(purpose),purpose,classification='public-redacted',classifier=lambda raw:'public-redacted')
            j,r=self.call(purpose,p,classification='public-redacted');self.assertEqual(len(j.payloads),1)

    def test_real_canonical_adapter_calls_mandatory_force_flags(self):
        import types
        calls=[]
        module=types.ModuleType('agent.redact')
        def canonical(text,**kwargs):calls.append(kwargs);return F.redact(text)
        module.redact_sensitive_text=canonical
        with patch.dict(sys.modules,{'agent':types.ModuleType('agent'),'agent.redact':module}), \
             patch.object(O,'canonical_redact',REAL_CANONICAL_REDACT):
            for purpose in ('context_reranking','semantic_cascade'):
                j,r=self.call(purpose,F.admission(self.requests(purpose),purpose))
                self.assertEqual(len(j.payloads),1)
        self.assertTrue(calls)
        self.assertTrue(all(k=={'force':True,'redact_url_credentials':True} for k in calls))
    def test_real_missing_canonical_import_zero_calls(self):
        with patch.dict(sys.modules,{'agent.redact':None}),patch.object(O,'canonical_redact',REAL_CANONICAL_REDACT):
            for purpose in ('context_reranking','semantic_cascade'):
                j,r=self.call(purpose,F.admission(self.requests(purpose),purpose));self.assertEqual(j.payloads,[])
    def test_expired_between_preparation_and_send_zero_calls(self):
        import time
        purpose='semantic_cascade';requests=self.requests(purpose)
        now=time.time();policy=F.admission(requests,purpose,grants=[F.grant(*requests[0],purpose,expires=now+10)])
        with patch('typed_jev.time.time',return_value=now):
            prepared=policy.prepare(requests,purpose=purpose,classification='synthetic',documents=[])[0]
        j=Recorder()
        with patch('typed_jev.time.time',return_value=now+11):
            with self.assertRaises(O.AdmissionError):policy.evaluate(prepared,j,purpose=purpose,classification='synthetic')
        self.assertEqual(j.payloads,[])
    def test_redacted_prepared_bytes_tamper_zero_calls(self):
        from dataclasses import replace
        purpose='semantic_cascade';requests=self.requests(purpose);policy=F.admission(requests,purpose)
        prepared=policy.prepare(requests,purpose=purpose,classification='synthetic',documents=[])[0]
        prepared=replace(prepared,body=prepared.body.replace(b'[REDACTED]',b'CHANGED'))
        j=Recorder()
        with self.assertRaises(O.AdmissionError):policy.evaluate(prepared,j,purpose=purpose,classification='synthetic')
        self.assertEqual(j.payloads,[])

if __name__=='__main__':unittest.main()
