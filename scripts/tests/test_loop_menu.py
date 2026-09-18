"""Closed loop menu: Jev selects one pre-admitted stage or returns to the parent."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import outbound_fixtures as OF
try:import loop_menu as M
except ImportError:M=None

CONTROLS=['return_to_parent','stop']


class Judge:
    """Counting stub provider. Never a real model and never calibrated.

    The wire payload is canonicalized before it reaches a provider, so the
    criteria arrive sorted; the choice is named explicitly rather than
    positionally.
    """
    def __init__(self,choice='stage-a',confidence=.99,top=.99):
        self.calls=0;self.choice=choice;self.confidence=confidence;self.top=top
    def evaluate(self,state,questions):
        self.calls+=1
        criteria=questions['next_stage']['criteria'];names=list(criteria)
        assert self.choice in names,'stub provider may only answer inside the closed menu'
        rest=(1-self.top)/max(1,len(names)-1)
        probabilities={k:(self.top if k==self.choice else rest) for k in names}
        if len(names)==1:probabilities={names[0]:1.0}
        return {'model':'jev-1.13.0','answers':{'next_stage':{'type':'choice','choice':self.choice,
                'confidence':self.confidence,'probabilities':probabilities}},
                'usage':{'input_tokens':7,'output_tokens':3}}


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(M,'bounded loop menu missing')

    def context(self,**over):
        ctx={'goal_hash':'a'*64,'iteration':0,'max_iterations':2,'last_status':'none','last_accepted':False,
             'scope_violations':[],'remaining_stage_ids':['stage-a','stage-b'],'hard_owner_boundary':False}
        ctx.update(over);return ctx

    def admitted(self,ctx):
        questions=M._questions(M.allowed(ctx))
        return OF.admission([({'loop_context':ctx},questions)],'stage_transition')

    def test_menu_is_closed(self):
        ctx=self.context()
        self.assertEqual(M.allowed(ctx),['stage-a','stage-b']+CONTROLS)
        self.assertEqual(list(M.CONTROLS),CONTROLS)

    def test_unknown_field_rejected(self):
        ctx=self.context();ctx['permissions']=['all']
        with self.assertRaises(M.ContractError):M.allowed(ctx)
        with self.assertRaises(M.ContractError):M.choose(ctx)

    def test_microrouting_rejected(self):
        ctx=self.context()
        for action in ('read','edit','run_tests','deploy','next_stage:anything'):
            with self.assertRaises(M.ContractError):M.choose(ctx,proposed=action)

    def test_scope_violation_returns_parent(self):
        ctx=self.context(iteration=1,last_accepted=True,scope_violations=['unrelated.txt'])
        self.assertEqual(M.allowed(ctx),CONTROLS)
        with self.assertRaises(M.ContractError):M.choose(ctx,proposed='stage-b')

    def test_unaccepted_previous_iteration_blocks_continue(self):
        ctx=self.context(iteration=1,last_accepted=False,last_status='iteration_needs_review')
        self.assertEqual(M.allowed(ctx),CONTROLS)

    def test_budget_exhausted_blocks_continue(self):
        ctx=self.context(iteration=2,max_iterations=2,last_accepted=True,last_status='ok')
        self.assertEqual(M.allowed(ctx),CONTROLS)
        ctx=self.context(iteration=1,last_accepted=True,remaining_stage_ids=[])
        self.assertEqual(M.allowed(ctx),CONTROLS)

    def test_hard_owner_boundary_blocks_continue(self):
        ctx=self.context(hard_owner_boundary=True)
        self.assertEqual(M.allowed(ctx),CONTROLS)

    def test_low_confidence_returns_parent(self):
        ctx=self.context();judge=Judge(confidence=.1,top=.4)  # would otherwise select stage-a
        with patch('outbound_admission.canonical_redact',OF.redact):
            r=M.choose(ctx,jev=judge,admission=self.admitted(ctx))
        # Causal: the provider was reached and its answer was refused on threshold.
        self.assertEqual(judge.calls,1)
        self.assertEqual(r['decision'],'return_to_parent')
        self.assertEqual(r['reason'],'parent_default')
        self.assertEqual(r['usage'],{'input_tokens':7,'output_tokens':3})
        self.assertFalse(r['parent_acceptance'])

    def test_high_confidence_selects_pre_admitted_stage(self):
        # Control for the two negative provider tests: the admitted path can succeed.
        ctx=self.context();judge=Judge()
        with patch('outbound_admission.canonical_redact',OF.redact):
            r=M.choose(ctx,jev=judge,admission=self.admitted(ctx))
        self.assertEqual(judge.calls,1)
        self.assertEqual(r['decision'],'stage-a')
        self.assertEqual(r['source'],'jev')

    def test_missing_provider_returns_parent(self):
        r=M.choose(self.context())
        self.assertEqual(r['decision'],'return_to_parent');self.assertEqual(r['source'],'policy')
        self.assertIsNone(r['provider_model'])

    def test_no_admission_no_send(self):
        judge=Judge()
        r=M.choose(self.context(),jev=judge,admission=None)
        self.assertEqual(judge.calls,0)
        self.assertEqual(r['decision'],'return_to_parent');self.assertEqual(r['source'],'policy')


if __name__=='__main__':unittest.main()
