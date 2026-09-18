from pathlib import Path
import sys
import unittest
SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
try:import stage_transition as T
except ImportError:T=None
class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(T,'bounded transition implementation missing')
        self.ctx={'checks_passed':False,'correction_used':False,'parent_authorized_correction':True,
          'evidence_hash':'a'*64,'previous_evidence_hash':None,'scope_violations':[],
          'triage_category':'implementation_defect','remaining_worker_calls':1,'hard_owner_boundary':False}
    def test_whitelist(self):
        self.assertEqual(set(T.allowed(self.ctx)),{'one_bounded_correction','return_to_owner','clarify','stop'})
    def test_only_one_correction(self):
        self.ctx['correction_used']=True
        self.assertNotIn('one_bounded_correction',T.allowed(self.ctx))
    def test_repeat_without_new_evidence(self):
        self.ctx['previous_evidence_hash']='a'*64
        self.assertNotIn('one_bounded_correction',T.allowed(self.ctx))
    def test_parent_must_authorize_correction(self):
        self.ctx['parent_authorized_correction']=False
        self.assertNotIn('one_bounded_correction',T.allowed(self.ctx))
    def test_unknown_defect_no_autofix(self):
        self.ctx['triage_category']='unknown'
        self.assertNotIn('one_bounded_correction',T.allowed(self.ctx))
    def test_scope_violation_no_autofix(self):
        self.ctx['scope_violations']=['other.py']
        self.assertNotIn('one_bounded_correction',T.allowed(self.ctx))
    def test_microrouting_rejected(self):
        for action in ('read','edit','test','replan','deploy'):
            with self.assertRaises(T.ContractError):T.choose(self.ctx,proposed=action)
    def test_no_permission_expansion_field(self):
        self.ctx['permissions']=['all']
        with self.assertRaises(T.ContractError):T.allowed(self.ctx)
    def test_complete_only_after_checks(self):
        self.assertNotIn('complete',T.allowed(self.ctx));self.ctx['checks_passed']=True
        r=T.choose(self.ctx,proposed='complete');self.assertEqual(r['transition'],'complete');self.assertFalse(r['parent_acceptance'])
    def test_low_confidence_returns_owner(self):
        class J:
            def evaluate(self,s,q):
                criteria=q['next_stage']['criteria'];n=len(criteria)
                return {'model':'jev-1.13.0','answers':{'next_stage':{'type':'choice','choice':next(iter(criteria)),
                        'confidence':.1,'probabilities':{k:1/n for k in criteria}}},'usage':{'input_tokens':1,'output_tokens':1}}
        self.assertEqual(T.choose(self.ctx,jev=J())['transition'],'return_to_owner')
    def test_hard_risk_no_complete_or_correction(self):
        self.ctx.update(hard_owner_boundary=True,checks_passed=True)
        self.assertNotIn('complete',T.allowed(self.ctx));self.assertNotIn('one_bounded_correction',T.allowed(self.ctx))
if __name__=='__main__':unittest.main()
