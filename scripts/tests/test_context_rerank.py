import copy
from pathlib import Path
import sys
import tempfile
import unittest
SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import routing_fixtures as F
from outbound_fixtures import select_context
try:import context_rerank as R
except ImportError:R=None
class Scores:
    def __init__(self,scores):self.scores=scores;self.calls=0
    def evaluate(self,state,questions):
        self.calls+=1
        return {'model':'jev-1.13.0','answers':{k:{'type':'noul','noul':self.scores.get(k.removeprefix('pair.'),.95)} for k in questions},
                'usage':{'input_tokens':4,'output_tokens':2}}
class RerankTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(R,'context reranker missing')
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
        self.candidates=[]
        for k,text in [('a','Mandatory signature rule.\n'),('b','Addition behavior and test details.\n'),('c','Unrelated historical note.\n')]:
            p=self.root/(k+'.txt');p.write_text(text)
            self.candidates.append({'id':k,'source_path':str(p),'source_sha256':F.sha(p),'start_line':1,'end_line':1,'mandatory':k=='a'})
        self.ctx={'candidates':self.candidates,'mandatory_ids':['a'],'top_k':1}
    def test_mandatory_union(self):
        r=select_context('addition',self.ctx,Scores({'a':.01,'b':.99,'c':.01}),max_chars=1000)
        self.assertEqual(r['status'],'selected');self.assertEqual(r['selected_ids'],['a','b'])
    def test_low_confidence_expands_not_drops(self):
        r=select_context('addition',self.ctx,Scores({'a':.01,'b':.55,'c':.01}),max_chars=1000)
        self.assertEqual(set(r['selected_ids']),{'a','b','c'})
    def test_tie_deterministic(self):
        r=select_context('addition',self.ctx,Scores({'a':.01,'b':.99,'c':.99}),max_chars=1000)
        self.assertEqual(r['selected_ids'],['a','b'])
    def test_reordered_input_same_selection(self):
        r=select_context('addition',self.ctx,Scores({'a':.01,'b':.99,'c':.99}),max_chars=1000)
        self.ctx['candidates'].reverse()
        s=select_context('addition',self.ctx,Scores({'a':.01,'b':.99,'c':.99}),max_chars=1000)
        self.assertEqual(r['selected_ids'],s['selected_ids'])
    def test_changed_source_blocks(self):
        Path(self.candidates[0]['source_path']).write_text('changed')
        r=select_context('addition',self.ctx,Scores({}),max_chars=1000)
        self.assertEqual(r['status'],'needs_review');self.assertEqual(r['selected'],[])
    def test_mutation_during_provider_blocks(self):
        path=Path(self.candidates[0]['source_path'])
        class Mutate(Scores):
            def evaluate(self,s,q):path.write_text('changed');return super().evaluate(s,q)
        r=select_context('addition',self.ctx,Mutate({}),max_chars=1000)
        self.assertEqual(r['status'],'needs_review')
    def test_missing_mandatory_blocks(self):
        self.ctx['mandatory_ids'].append('missing')
        self.assertEqual(select_context('task',self.ctx,Scores({}),max_chars=1000)['status'],'needs_review')
    def test_no_candidate_is_explicit(self):
        self.ctx.update(candidates=[],mandatory_ids=[])
        r=select_context('task',self.ctx,Scores({}),max_chars=1000)
        self.assertEqual(r['status'],'needs_review');self.assertEqual(r['reason'],'no_candidates')
    def test_budget_does_not_truncate_mandatory(self):
        r=select_context('task',self.ctx,Scores({}),max_chars=5)
        self.assertEqual(r['status'],'needs_review');self.assertEqual(r['selected'],[])
    def test_no_provider_preserves_all(self):
        r=select_context('task',self.ctx,None,max_chars=1000)
        self.assertEqual(set(r['selected_ids']),{'a','b','c'});self.assertFalse(r['rerank_applied'])
    def test_exhausted_call_budget_preserves_all(self):
        j=Scores({});r=select_context('task',self.ctx,j,max_chars=1000,max_calls=0)
        self.assertEqual(j.calls,0);self.assertEqual(len(r['selected_ids']),3)
    def test_batching_and_full_scores(self):
        j=Scores({});r=select_context('task',self.ctx,j,max_chars=1000,batch_size=1)
        self.assertEqual(j.calls,3);self.assertEqual(set(r['scores']),{'a','b','c'});self.assertEqual(len(r['shortlist']),3)
    def test_unknown_answer_expands_safely(self):
        class Wrong:
            def evaluate(self,s,q):return {'model':'jev-1.13.0','answers':{'invented':{'type':'noul','noul':1}},'usage':{'input_tokens':1,'output_tokens':1}}
        r=select_context('task',self.ctx,Wrong(),max_chars=1000)
        self.assertEqual(len(r['selected_ids']),3);self.assertFalse(r['rerank_applied'])
    def test_duplicate_candidate_blocks(self):
        self.ctx['candidates'].append(copy.deepcopy(self.candidates[0]))
        self.assertEqual(select_context('task',self.ctx,None,max_chars=1000)['status'],'needs_review')
if __name__=='__main__':unittest.main()
