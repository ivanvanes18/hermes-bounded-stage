"""Hash-bound loop over pre-admitted stages: ledger, iteration, continuation.

Synthetic only. No live executor, no Luna admission, no production activation and
no acceptance of any artifact: `checkpoint_required` means the parent must look.
"""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import loop_fixtures as L
import outbound_fixtures as OF
import executor_routes as R
import executor_runtime as E
import harness_adapter as H
from schema_validation import ContractError,canonical,digest
try:
    import bounded_loop as B
    import loop_menu as M
except ImportError:B=M=None

CONTROLS=['return_to_parent','stop']


class LoopBase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(B,'bounded loop controller missing')
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name)
        self.env=L.envelope(self.root);self.env2=L.envelope_2(self.root,self.env)
        self.reg=L.registry(self.root)
        self.work=Path(self.env['workspace']);self.loop=self.root/'loop'

    # --- helpers -------------------------------------------------------------
    def reason(self,call,*args,**kw):
        with self.assertRaises(ContractError) as caught:call(*args,**kw)
        return str(caught.exception)

    def crash_before(self,kind):
        """Deterministic crash in the window between artifact and commit."""
        real=B._commit
        def failing(loop,records,state,commit_kind,**fields):
            if commit_kind==kind:raise RuntimeError('crash before commit: '+commit_kind)
            return real(loop,records,state,commit_kind,**fields)
        return patch.object(B,'_commit',side_effect=failing)

    def init(self,envelope=None,registry=None,loop=None,**kw):
        return B.initialize(envelope if envelope is not None else self.env,
                            registry if registry is not None else self.reg,
                            loop if loop is not None else self.loop,
                            evidence_mode=kw.pop('evidence_mode','synthetic'),**kw)

    def read(self,relative,loop=None):
        return json.loads((Path(loop or self.loop)/relative).read_text())

    def records(self,loop=None):
        directory=Path(loop or self.loop)/'transitions'
        return [json.loads(p.read_text()) for p in sorted(directory.iterdir())]

    def tree(self,base):
        base=Path(base)
        return {str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(base.rglob('*')) if p.is_file()}

    def advance(self,stage='synthetic-stage',route='fixture_worker',**kw):
        return B.advance(self.loop,fixed_stage=stage,fixed_route=route,**kw)

    def run_dir(self,index=1,loop=None):
        return Path(loop or self.loop)/('iterations/%d/run'%index)

    def accept_run(self,index=1,loop=None):
        run=self.run_dir(index,loop);view=E.inspect(run)
        return E.accept(run,{'decision':'accept','evidence_hash':view['result_hash'],
                             'reviewer':'synthetic-parent','evidence_kind':'synthetic'})

    def continue_approval(self,loop=None):
        entry=B.inspect(loop or self.loop)['iterations'][-1]
        return {'decision':'continue','evidence_hash':entry['result_hash'],
                'reviewer':'synthetic-parent','evidence_kind':'synthetic'}

    def redirect_approval(self,loop=None,work=None):
        entry=B.inspect(loop or self.loop)['iterations'][-1]
        return {'decision':'redirect','evidence_hash':entry['result_hash'],
                'carried_tree_hash':digest(E.snapshot(work or self.work)),
                'reviewer':'synthetic-parent','evidence_kind':'synthetic'}

    def redirect(self,new_loop,envelope=None,registry=None,**kw):
        return B.redirect(self.loop,new_loop,envelope if envelope is not None else self.env2,
                          registry if registry is not None else self.reg,
                          evidence_mode=kw.pop('evidence_mode','synthetic'),**kw)


class EnvelopeAndGenesisTests(LoopBase):
    """1b — envelope contract, seal and ledger genesis (11-18)."""

    def test_envelope_duplicate_stage_id_refused(self):
        env=copy.deepcopy(self.env);env['stages'][1]['stage_id']=env['stages'][0]['stage_id']
        self.assertEqual(self.reason(B.validate_envelope,env),'duplicate_stage_identity')

    def test_reserved_stage_identity_refused(self):
        env=copy.deepcopy(self.env);env['stages'][1]['stage_id']='stop'
        self.assertEqual(self.reason(B.validate_envelope,env),'reserved_stage_identity')
        env['stages'][1]['stage_id']='return_to_parent'
        self.assertEqual(self.reason(B.validate_envelope,env),'reserved_stage_identity')

    def test_stage_workspace_mismatch_refused(self):
        env=copy.deepcopy(self.env);env['stages'][1]['workspace']=str(self.root)
        self.assertEqual(self.reason(B.validate_envelope,env),'stage_workspace_mismatch')

    def test_stage_budget_exceeds_loop_refused(self):
        env=copy.deepcopy(self.env);env['stages'][0]['limits']['max_seconds']=env['max_seconds']+1
        self.assertEqual(self.reason(B.validate_envelope,env),'stage_budget_exceeds_loop')

    def test_future_stage_input_in_prior_allowed_paths_refused(self):
        # Control: the shipped fixture envelope is admissible as written.
        B.validate_envelope(self.env)
        env=copy.deepcopy(self.env)
        env['stages'][1]['inputs']=[{'id':'source','path':'sample.py','sha256':L.sha(self.work/'sample.py')}]
        self.assertEqual(self.reason(B.validate_envelope,env),'stage_input_mutated_by_earlier_stage')

    def test_loop_dir_create_only(self):
        self.init()
        self.assertEqual(self.reason(self.init),'loop_dir_exists')

    def test_loop_destination_boundaries(self):
        self.assertEqual(self.reason(self.init,loop=self.work/'control'),'loop_inside_workspace')
        self.assertFalse((self.work/'control').exists())
        # A symlinked parent component is refused before any directory is made.
        real=self.root/'real';real.mkdir();link=self.root/'link';link.symlink_to(real)
        self.assertIn(self.reason(self.init,loop=link/'loop'),('symlink_refused','noncanonical_path'))
        self.assertFalse((real/'loop').exists())
        # A symlinked destination is never followed, even to a missing target.
        target=self.root/'target';dest=self.root/'dest';dest.symlink_to(target)
        self.assertEqual(self.reason(self.init,loop=dest),'loop_dir_exists')
        self.assertFalse(target.exists())

    def test_genesis_layout_and_projection(self):
        view=self.init()
        for name in ('loop.json','registry.json','loop-seal.json','loop-state.json',
                     'transitions/0000-genesis.json'):
            self.assertTrue((self.loop/name).is_file(),name)
        seal=self.read('loop-seal.json');state=self.read('loop-state.json')
        genesis=self.read('transitions/0000-genesis.json')
        self.assertEqual(seal['loop_hash'],digest(self.read('loop.json')))
        self.assertEqual(seal['registry_hash'],digest(self.read('registry.json')))
        self.assertEqual(seal['loop_hash'],digest(self.env))
        self.assertEqual(seal['initial_tree_hash'],digest(E.snapshot(self.work)))
        self.assertIsNone(seal['continuation'])
        self.assertEqual(genesis['kind'],'genesis');self.assertEqual(genesis['sequence'],0)
        self.assertIsNone(genesis['previous_transition_hash'])
        self.assertIsNone(genesis['before_state_hash'])
        self.assertEqual(genesis['seal_hash'],digest(seal))
        self.assertEqual(digest(state),genesis['after_state_hash'])
        self.assertFalse(genesis['parent_acceptance'])
        self.assertEqual(state['seal_hash'],digest(seal))
        self.assertEqual((view['status'],view['iteration'],view['reason']),('ready',0,'initialized'))
        self.assertEqual(B.inspect(self.loop)['status'],'ready')


class IterationTests(LoopBase):
    """1c — one bounded iteration, ledger integrity and recovery (19-29)."""

    def test_first_iteration_runs_pinned_adapter_and_checks(self):
        self.init();view=self.advance()
        self.assertEqual(view['status'],'checkpoint_required')
        self.assertEqual(view['reason'],'iteration_ready_for_parent_review')
        self.assertIn('return a + b',(self.work/'sample.py').read_text())
        run=E.inspect(self.loop/'iterations/1/run')
        self.assertEqual(run['attempts'],1)
        self.assertEqual(run['status'],'ready_for_parent_review')
        self.assertFalse(run['parent_accepted'])
        self.assertEqual([r['kind'] for r in self.records()],
                         ['genesis','iteration_started','iteration_recorded'])
        entry=view['iterations'][0]
        self.assertEqual(entry['index'],1)
        self.assertEqual(entry['stage_id'],'synthetic-stage')
        self.assertEqual(entry['run_relative'],'iterations/1/run')
        self.assertEqual(entry['result_hash'],run['result_hash'])
        self.assertEqual(entry['tree_hash'],digest(E.snapshot(self.work)))
        self.assertFalse(entry['accepted'])
        receipt=self.read('selection-receipt-1.json')
        self.assertEqual((receipt['kind'],receipt['decision'],receipt['source']),
                         ('selection','synthetic-stage','parent'))
        self.assertFalse(receipt['parent_acceptance'])
        self.assertEqual(digest(self.read('loop-state.json')),self.records()[-1]['after_state_hash'])

    def test_worker_unreachable_without_fixed_route_or_admission(self):
        self.init()
        with patch.object(H,'execute',side_effect=AssertionError('no worker without a route')) as worker:
            view=self.advance(route=None)
            worker.assert_not_called()
        self.assertEqual(view['status'],'checkpoint_required')
        self.assertEqual(view['reason'],'iteration_needs_review')
        receipt=self.read('iterations/1/run/control-route-receipt.json')
        self.assertEqual(receipt['selected_route'],'owner')
        self.assertEqual(receipt['reason'],'provider_unavailable')
        self.assertEqual(receipt['source'],'policy')

    def test_deterministic_failure_precedes_semantic(self):
        env=copy.deepcopy(self.env);stage=env['stages'][0]
        stage['features']['semantic_cascade']=True
        stage['semantic_flags']=[{'id':'unsupported_value','source_id':'source','output_path':'sample.py'}]
        self.init(envelope=env,registry=L.registry(self.root,'bad_result'))
        judge=L.Judge();view=self.advance(providers={'semantic_cascade':judge})
        self.assertEqual(judge.calls,0)
        self.assertEqual(self.read('iterations/1/run/attempts/1/semantic-receipt.json')['status'],
                         'blocked_deterministic')
        self.assertEqual(view['reason'],'iteration_needs_review')

    def test_one_bounded_correction_inside_iteration(self):
        env=copy.deepcopy(self.env);env['stages'][0]['features']['stage_transition']=True
        reg=copy.deepcopy(self.reg);L.attach_retry_adapter(self.root,reg)
        self.init(envelope=env,registry=reg)
        self.assertEqual(self.advance()['reason'],'iteration_needs_review')
        run=self.loop/'iterations/1/run'
        E.transition(run,proposed='one_bounded_correction',parent_authorized_correction=True,
                     triage_category='implementation_defect')
        after=E.advance(run,fixed_route='fixture_worker')
        self.assertEqual(after['attempts'],2)
        self.assertEqual(after['status'],'ready_for_parent_review')
        # The correction happened outside the loop's recorded evidence, so the
        # loop refuses to advance rather than growing its correction count.
        with patch.object(H,'execute',side_effect=AssertionError('no worker')) as worker:
            self.assertEqual(self.reason(self.advance),'iteration_evidence_mismatch')
            worker.assert_not_called()

    def test_scope_violation_returns_to_parent_not_next_stage(self):
        self.init(registry=L.registry(self.root,'scope_violation'))
        view=self.advance()
        self.assertEqual(view['status'],'checkpoint_required')
        self.assertEqual(view['reason'],'scope_ambiguity_returned_to_parent')
        self.assertEqual(view['allowed'],CONTROLS)
        result=E.inspect(self.loop/'iterations/1/run')['last_result']
        self.assertIn('unrelated.txt',result['scope_violations'])

    def test_luna_routes_remain_unavailable_in_loop(self):
        env=copy.deepcopy(self.env);env['stages'][0]['executor_candidates']=['luna_medium','owner']
        self.assertEqual(R.available(env['stages'][0],self.reg,{}),['owner'])
        self.init(envelope=env)
        with patch.object(H,'execute',side_effect=AssertionError('luna is not admitted')) as worker:
            view=self.advance(route=None)
            worker.assert_not_called()
        self.assertEqual(view['status'],'checkpoint_required')
        receipt=self.read('iterations/1/run/control-route-receipt.json')
        self.assertEqual(receipt['selected_route'],'owner')
        self.assertEqual(receipt['reason'],'no_ready_executor')

    def test_workspace_lock_not_held_across_iteration(self):
        self.init();marker=self.work/'.staged-routing-workspace.json';seen=[];real=E.initialize
        def probing(*args,**kw):
            # flock is per open file description, so a marker still held by
            # bounded_loop.advance fails this acquisition with the same
            # one_writer_lock_busy that executor_runtime.initialize would hit.
            fd=os.open(marker,os.O_RDONLY)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);seen.append('free')
                fcntl.flock(fd,fcntl.LOCK_UN)
            finally:os.close(fd)
            return real(*args,**kw)
        with patch.object(E,'initialize',side_effect=probing):
            view=self.advance()
        self.assertEqual(seen,['free'])
        self.assertEqual(view['status'],'checkpoint_required')

    def test_interrupted_iteration_no_implicit_retry(self):
        self.init()
        with patch.object(E,'initialize',side_effect=RuntimeError('crash after write-ahead')):
            with self.assertRaises(RuntimeError):self.advance()
        self.assertEqual([r['kind'] for r in self.records()],['genesis','iteration_started'])
        self.assertEqual(self.read('loop-state.json')['status'],'iteration_running')
        with patch.object(H,'execute',side_effect=AssertionError('no implicit retry')) as worker:
            view=self.advance()
            worker.assert_not_called()
        self.assertEqual(view['status'],'checkpoint_required')
        self.assertEqual(view['reason'],'interrupted_iteration_no_implicit_retry')
        self.assertEqual([r['kind'] for r in self.records()],
                         ['genesis','iteration_started','interrupted'])

    def test_crash_between_record_and_projection_recovers(self):
        self.init();zero=self.read('loop-state.json');self.advance()
        records=self.records()
        previous=B._apply(zero,records[1])
        self.assertEqual(digest(previous),records[2]['before_state_hash'])
        (self.loop/'loop-state.json').write_bytes(canonical(previous)+b'\n')
        self.assertEqual(self.reason(B.inspect,self.loop),'loop_projection_stale')
        for expected in ('projection_replayed','projection_current'):
            with patch.object(H,'execute') as worker,patch.object(E,'advance') as runner,\
                 patch.object(E,'initialize') as starter,patch.object(M,'choose') as menu:
                self.assertEqual(B.recover(self.loop)['status'],expected)
                worker.assert_not_called();runner.assert_not_called()
                starter.assert_not_called();menu.assert_not_called()
            self.assertEqual(digest(self.read('loop-state.json')),records[-1]['after_state_hash'])
        # A current projection is still bound to the persisted run it names.
        path=self.loop/'iterations/1/run/state.json';value=json.loads(path.read_text())
        value['result_hash']='0'*64;path.write_text(json.dumps(value))
        self.assertEqual(self.reason(B.recover,self.loop),'iteration_evidence_mismatch')

    def test_state_only_edit_refused_before_action(self):
        self.init();self.advance()
        path=self.loop/'loop-state.json';state=json.loads(path.read_text())
        state['iterations'][0]['accepted']=True;path.write_text(json.dumps(state))
        with patch.object(H,'execute',side_effect=AssertionError('no action on a stale projection')) as worker:
            self.assertEqual(self.reason(B.inspect,self.loop),'loop_projection_stale')
            self.assertEqual(self.reason(self.advance),'loop_projection_stale')
            self.assertEqual(self.reason(B.recover,self.loop),'loop_projection_unrecoverable')
            worker.assert_not_called()

    def test_ledger_and_seal_tamper_refused(self):
        self.init();self.advance()
        directory=self.loop/'transitions'
        names=sorted(path.name for path in directory.iterdir())
        self.assertEqual(len(names),3)
        original={name:(directory/name).read_bytes() for name in names}
        # Editing any record breaks the forward chain of its successor.
        record=json.loads(original[names[1]]);record['recorded_at']+=1
        (directory/names[1]).write_text(json.dumps(record))
        self.assertEqual(self.reason(B.inspect,self.loop),'transition_chain_broken')
        (directory/names[1]).write_bytes(original[names[1]])
        # A hole in the sequence is a gap.
        (directory/names[1]).unlink()
        self.assertEqual(self.reason(B.inspect,self.loop),'transition_ledger_gap')
        (directory/names[1]).write_bytes(original[names[1]])
        # Removing the newest record leaves a shorter, internally valid chain.
        # Only the projection reveals it — that is what load step 7 is for.
        (directory/names[2]).unlink()
        self.assertEqual(self.reason(B.inspect,self.loop),'loop_projection_stale')
        (directory/names[2]).write_bytes(original[names[2]])
        # An out-of-sequence record file is a gap.
        extra=directory/'0007-checkpoint.json';extra.write_bytes(original[names[0]])
        self.assertEqual(self.reason(B.inspect,self.loop),'transition_ledger_gap')
        extra.unlink()
        self.assertEqual(B.inspect(self.loop)['status'],'checkpoint_required')
        # The sealed envelope and registry are bound by hash.
        for name in ('loop.json','registry.json'):
            path=self.loop/name;raw=path.read_bytes();value=json.loads(raw)
            value['__tamper__']='x';path.write_text(json.dumps(value))
            self.assertEqual(self.reason(B.inspect,self.loop),'sealed_loop_changed')
            path.write_bytes(raw)
        self.assertEqual(B.inspect(self.loop)['status'],'checkpoint_required')


class CheckpointAndContinuationTests(LoopBase):
    """1d — parent checkpoint, immutable continuation, acceptance (30-40)."""

    def test_continue_requires_accepted_iteration(self):
        self.init();self.advance()
        self.assertEqual(self.reason(B.checkpoint,self.loop,'continue',
                                     approval=self.continue_approval()),
                         'continue_requires_accepted_evidence')
        # The same checkpoint may always be stopped instead.
        view=B.checkpoint(self.loop,'stop')
        self.assertEqual(view['status'],'stopped')
        self.assertEqual(self.reason(self.advance),'loop_terminal')

    def test_budget_blocks_third_iteration(self):
        env=copy.deepcopy(self.env);env['max_iterations']=2
        self.init(envelope=env);self.advance();self.accept_run(1)
        view=B.checkpoint(self.loop,'continue',approval=self.continue_approval())
        self.assertEqual((view['status'],view['iteration']),('ready',1))
        self.assertEqual(view['allowed'],['synthetic-stage-b']+CONTROLS)
        view=self.advance(stage='synthetic-stage-b',route='deterministic')
        self.assertEqual(view['reason'],'iteration_ready_for_parent_review')
        self.assertTrue((self.work/'notes.md').is_file())
        self.accept_run(2)
        view=B.checkpoint(self.loop,'continue',approval=self.continue_approval())
        self.assertEqual((view['status'],view['iteration']),('ready',2))
        self.assertEqual(view['allowed'],CONTROLS)
        with patch.object(H,'execute',side_effect=AssertionError('budget')) as worker:
            self.assertEqual(self.reason(self.advance),'loop_iteration_budget')
            worker.assert_not_called()

    def test_redirect_requires_accepted_evidence(self):
        self.init();self.advance()
        self.assertEqual(self.reason(self.redirect,self.root/'loop-2',
                                     approval=self.redirect_approval()),
                         'redirect_requires_accepted_evidence')
        self.assertFalse((self.root/'loop-2').exists())

    def test_redirect_requires_unchanged_workspace(self):
        self.init();self.advance();self.accept_run(1)
        approval=self.redirect_approval()
        (self.work/'sample.py').write_text('changed after acceptance\n')
        self.assertEqual(self.reason(self.redirect,self.root/'loop-2',approval=approval),
                         'redirect_workspace_changed')
        self.assertFalse((self.root/'loop-2').exists())

    def test_redirect_creates_immutable_continuation(self):
        self.init();self.advance();self.accept_run(1)
        approval=self.redirect_approval();before=self.tree(self.loop);new_loop=self.root/'loop-2'
        result=self.redirect(new_loop,approval=approval)
        self.assertEqual(result['status'],'redirected')
        after=self.tree(self.loop)
        immutable={path for path in before
                   if path in ('loop.json','registry.json','loop-seal.json')
                   or path.startswith(('transitions/','iterations/')) or 'receipt' in path}
        self.assertIn('selection-receipt-1.json',immutable)
        for path in immutable:self.assertEqual(before[path],after[path],path)
        self.assertEqual(set(after)-set(before),
                         {'redirect-receipt.json','transitions/0003-redirect.json'})
        self.assertEqual({p for p in set(before)&set(after) if before[p]!=after[p]},
                         {'loop-state.json'})
        entry=B.inspect(self.loop)['iterations'][-1]
        seal=self.read('loop-seal.json',new_loop);continuation=seal['continuation']
        self.assertEqual(continuation['carried_result_hash'],entry['result_hash'])
        self.assertEqual(continuation['carried_tree_hash'],seal['initial_tree_hash'])
        self.assertEqual(continuation['approved_by'],'parent')
        self.assertEqual(continuation['source_loop_hash'],self.read('loop-seal.json')['loop_hash'])
        self.assertEqual(self.read('transitions/0000-genesis.json',new_loop)['seal_hash'],digest(seal))
        self.assertEqual(B.inspect(new_loop)['status'],'ready')
        self.assertEqual(result['continuation_seal_hash'],digest(seal))
        self.assertEqual(digest(self.read('loop-state.json')),self.records()[-1]['after_state_hash'])

    def test_redirect_is_single_use(self):
        self.init();self.advance();self.accept_run(1)
        approval=self.redirect_approval()
        # The crash window between the create-only receipt and the ledger record
        # must not let a second redirect publish anything.
        B._create(self.loop/'redirect-receipt.json',{'schema_version':1})
        self.assertEqual(self.reason(self.redirect,self.root/'loop-2',approval=approval),
                         'redirect_already_recorded')
        self.assertFalse((self.root/'loop-2').exists())
        (self.loop/'redirect-receipt.json').unlink()
        self.redirect(self.root/'loop-2',approval=approval)
        self.assertEqual(self.reason(self.redirect,self.root/'loop-3',approval=approval),
                         'loop_terminal')
        self.assertFalse((self.root/'loop-3').exists())

    def test_redirect_rejects_foreign_workspace(self):
        self.init();self.advance();self.accept_run(1)
        other=self.root/'other';other.mkdir()
        (other/'.staged-routing-workspace.json').write_text(
            '{"purpose":"hermes-routing-staged","schema_version":1}')
        foreign=copy.deepcopy(self.env2);foreign['workspace']=str(other)
        for stage in foreign['stages']:stage['workspace']=str(other)
        self.assertEqual(self.reason(self.redirect,self.root/'loop-2',envelope=foreign,
                                     approval=self.redirect_approval()),
                         'continuation_workspace_mismatch')
        self.assertFalse((self.root/'loop-2').exists())

    def test_redirect_aborts_without_publishing_on_tree_drift(self):
        self.init();self.advance();self.accept_run(1)
        approval=self.redirect_approval();new_loop=self.root/'loop-2'
        real=E.snapshot(self.work)
        def drifting(root):
            # The post-snapshot (ii) is taken after the destination exists; the
            # workspace check and the pre-snapshot (i) both run before it.
            observed=dict(real)
            if new_loop.exists():observed['drift.txt']='0'*64
            return observed
        with patch.object(B,'snapshot',side_effect=drifting):
            self.assertEqual(self.reason(self.redirect,new_loop,approval=approval),
                             'continuation_tree_drift')
        self.assertFalse(new_loop.exists())
        self.assertFalse((self.loop/'redirect-receipt.json').exists())
        self.assertEqual([r['kind'] for r in self.records()],
                         ['genesis','iteration_started','iteration_recorded'])
        self.assertEqual(B.inspect(self.loop)['status'],'checkpoint_required')

    def test_redirect_does_not_nest_workspace_lock(self):
        # A successful redirect proves the single marker section completes.
        self.init();self.advance();self.accept_run(1)
        self.redirect(self.root/'loop-2',approval=self.redirect_approval())
        # An independently initialized source loop in its own workspace: with the
        # marker held externally, the one acquisition redirect makes must fail.
        other=tempfile.TemporaryDirectory();self.addCleanup(other.cleanup)
        root=Path(other.name);env=L.envelope(root);env2=L.envelope_2(root,env)
        reg=L.registry(root);work=Path(env['workspace']);loop=root/'loop'
        B.initialize(env,reg,loop,evidence_mode='synthetic')
        B.advance(loop,fixed_stage='synthetic-stage',fixed_route='fixture_worker')
        self.accept_run(1,loop)
        approval=self.redirect_approval(loop,work)
        fd=os.open(work/'.staged-routing-workspace.json',os.O_RDONLY)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.reason(B.redirect,loop,root/'loop-2',env2,reg,
                                         approval=approval,evidence_mode='synthetic'),
                             'one_writer_lock_busy')
        finally:os.close(fd)
        self.assertFalse((root/'loop-2').exists())
        self.assertFalse((loop/'redirect-receipt.json').exists())
        self.assertEqual(B.inspect(loop)['status'],'checkpoint_required')

    def test_loop_accept_is_parent_only_and_binds_evidence(self):
        self.init();self.advance();self.accept_run(1)
        with self.assertRaises(ContractError):
            B.accept(self.loop,{'decision':'accept','loop_evidence_hash':'0'*64,
                                'reviewer':'synthetic-parent','evidence_kind':'synthetic'})
        approval={'decision':'accept','loop_evidence_hash':digest(B.inspect(self.loop)['iterations']),
                  'reviewer':'synthetic-parent','evidence_kind':'synthetic'}
        view=B.accept(self.loop,approval)
        self.assertEqual(view['status'],'loop_accepted')
        self.assertFalse(view['parent_acceptance'])
        self.assertEqual([r['kind'] for r in self.records()][-1],'accept')
        for record in self.records():self.assertFalse(record['parent_acceptance'])
        receipts=sorted(self.loop.glob('*receipt*.json'))
        self.assertTrue(receipts)
        for path in receipts:self.assertFalse(json.loads(path.read_text())['parent_acceptance'])
        self.assertEqual(self.reason(B.accept,self.loop,approval),'loop_terminal')

    def test_iteration_evidence_mismatch_refused(self):
        self.init();self.advance()
        path=self.run_dir(1)/'state.json';raw=path.read_bytes();value=json.loads(raw)
        value['result_hash']='1'*64;path.write_text(json.dumps(value))
        self.assertEqual(self.reason(B.inspect,self.loop),'iteration_evidence_mismatch')
        self.assertEqual(self.reason(B.checkpoint,self.loop,'continue',
                                     approval={'decision':'continue','evidence_hash':'1'*64,
                                               'reviewer':'p','evidence_kind':'synthetic'}),
                         'iteration_evidence_mismatch')
        path.write_bytes(raw)
        # Out-of-band parent acceptance is monotone: the projection may lag and
        # the checkpoint is still reachable.
        self.accept_run(1)
        self.assertFalse(B.inspect(self.loop)['iterations'][-1]['accepted'])
        self.assertTrue(E.inspect(self.run_dir(1))['parent_accepted'])
        view=B.checkpoint(self.loop,'continue',approval=self.continue_approval())
        self.assertEqual(view['status'],'ready')
        self.assertTrue(B.inspect(self.loop)['iterations'][-1]['accepted'])


class PreCommitReconciliationTests(LoopBase):
    """A crash between a published pre-commit artifact and its ledger record.

    Every artifact written before the commit point must either be adopted on the
    next invocation — bound by exact canonical bytes to the same pending
    transition and the same ledger tip — or fail closed. Reconciliation never
    replays a worker, a deterministic action or a provider call.
    """

    def test_orphan_selection_receipt_reconciles_without_reselecting(self):
        self.init()
        with self.crash_before('iteration_started'):
            with self.assertRaises(RuntimeError):self.advance()
        receipt=self.loop/'selection-receipt-1.json';raw=receipt.read_bytes()
        self.assertEqual([r['kind'] for r in self.records()],['genesis'])
        self.assertEqual(self.read('loop-state.json')['status'],'ready')
        self.assertFalse((self.loop/'iterations/1').exists())
        # A receipt this invocation cannot prove it owns never unblocks the loop.
        forged=json.loads(raw);forged['reason']='forged'
        receipt.write_bytes(canonical(forged)+b'\n')
        with patch.object(H,'execute',side_effect=AssertionError('no action on a foreign receipt')) as worker:
            self.assertEqual(self.reason(self.advance),'selection_already_recorded')
            worker.assert_not_called()
        self.assertEqual([r['kind'] for r in self.records()],['genesis'])
        receipt.write_bytes(raw)
        # The pending selection is adopted: no second menu call, no second send.
        with patch.object(M,'choose',side_effect=AssertionError('no reselection')) as menu:
            view=self.advance()
            menu.assert_not_called()
        self.assertEqual(view['status'],'checkpoint_required')
        self.assertEqual(view['reason'],'iteration_ready_for_parent_review')
        self.assertEqual([r['kind'] for r in self.records()],
                         ['genesis','iteration_started','iteration_recorded'])
        self.assertEqual(receipt.read_bytes(),raw)
        self.assertEqual([p.name for p in sorted(self.loop.glob('selection-receipt-*.json'))],
                         ['selection-receipt-1.json'])
        self.assertEqual(digest(self.read('loop-state.json')),self.records()[-1]['after_state_hash'])

    def test_orphan_control_selection_receipt_reconciles(self):
        self.init()
        with self.crash_before('checkpoint'):
            with self.assertRaises(RuntimeError):B.advance(self.loop)
        self.assertTrue((self.loop/'selection-receipt-1.json').is_file())
        self.assertEqual([r['kind'] for r in self.records()],['genesis'])
        with patch.object(M,'choose',side_effect=AssertionError('no reselection')) as menu:
            view=B.advance(self.loop)
            menu.assert_not_called()
        self.assertEqual((view['status'],view['reason'],view['iteration']),
                         ('checkpoint_required','no_admissible_stage',0))
        self.assertEqual([r['kind'] for r in self.records()],['genesis','checkpoint'])
        self.assertEqual([p.name for p in sorted(self.loop.glob('selection-receipt-*.json'))],
                         ['selection-receipt-1.json'])

    def test_orphan_checkpoint_receipt_reconciles(self):
        self.init();self.advance();self.accept_run(1)
        approval=self.continue_approval();committed=[r['kind'] for r in self.records()]
        with self.crash_before('checkpoint'):
            with self.assertRaises(RuntimeError):B.checkpoint(self.loop,'continue',approval=approval)
        path=self.loop/'checkpoint-receipt-1.json';raw=path.read_bytes()
        self.assertEqual([r['kind'] for r in self.records()],committed)
        # A different decision at the same tip is not the pending one.
        self.assertEqual(self.reason(B.checkpoint,self.loop,'stop'),'checkpoint_already_recorded')
        self.assertEqual(path.read_bytes(),raw)
        self.assertEqual([r['kind'] for r in self.records()],committed)
        view=B.checkpoint(self.loop,'continue',approval=approval)
        self.assertEqual((view['status'],view['iteration']),('ready',1))
        self.assertEqual([r['kind'] for r in self.records()],committed+['checkpoint'])
        self.assertEqual(path.read_bytes(),raw)
        self.assertEqual([p.name for p in sorted(self.loop.glob('checkpoint-receipt-*.json'))],
                         ['checkpoint-receipt-1.json'])

    def test_orphan_redirect_receipt_and_continuation_reconcile(self):
        self.init();self.advance();self.accept_run(1)
        approval=self.redirect_approval();new_loop=self.root/'loop-2'
        committed=[r['kind'] for r in self.records()]
        with self.crash_before('redirect'):
            with self.assertRaises(RuntimeError):self.redirect(new_loop,approval=approval)
        receipt=self.loop/'redirect-receipt.json';raw=receipt.read_bytes()
        published=self.tree(new_loop);before=self.tree(self.loop)
        self.assertIn('loop-seal.json',published)
        self.assertEqual([r['kind'] for r in self.records()],committed)
        self.assertEqual(B.inspect(self.loop)['status'],'checkpoint_required')
        with patch.object(H,'execute',side_effect=AssertionError('no worker')) as worker,\
             patch.object(E,'initialize',side_effect=AssertionError('no run')) as starter:
            result=self.redirect(new_loop,approval=approval)
            worker.assert_not_called();starter.assert_not_called()
        self.assertEqual(result['status'],'redirected')
        # The published continuation is adopted byte-for-byte, never rebuilt.
        self.assertEqual(self.tree(new_loop),published)
        self.assertEqual(result['continuation_seal_hash'],digest(self.read('loop-seal.json',new_loop)))
        self.assertEqual(result['continuation_loop'],str(new_loop))
        self.assertEqual(receipt.read_bytes(),raw)
        after=self.tree(self.loop)
        self.assertEqual(set(after)-set(before),{'transitions/0003-redirect.json'})
        self.assertEqual({p for p in set(before)&set(after) if before[p]!=after[p]},{'loop-state.json'})
        self.assertEqual([r['kind'] for r in self.records()],committed+['redirect'])
        self.assertEqual(digest(self.read('loop-state.json')),self.records()[-1]['after_state_hash'])

    def test_unpublished_continuation_remnant_is_discarded_and_retry_converges(self):
        self.init();self.advance();self.accept_run(1)
        approval=self.redirect_approval();new_loop=self.root/'loop-2'
        # Exactly what a crash between the destination's creation and its seal
        # leaves behind: no seal, so nothing committed can ever name it.
        new_loop.mkdir(mode=0o700)
        (new_loop/'transitions').mkdir(mode=0o700);(new_loop/'iterations').mkdir(mode=0o700)
        (new_loop/'loop.json').write_bytes(canonical(self.env2)+b'\n')
        (new_loop/'registry.json').write_bytes(canonical(self.reg)+b'\n')
        result=self.redirect(new_loop,approval=approval)
        self.assertEqual(result['status'],'redirected')
        self.assertEqual(B.inspect(new_loop)['status'],'ready')
        seal=self.read('loop-seal.json',new_loop)
        self.assertEqual(seal['continuation']['carried_tree_hash'],seal['initial_tree_hash'])
        self.assertEqual([p.name for p in sorted((new_loop/'transitions').iterdir())],
                         ['0000-genesis.json'])

    def test_foreign_continuation_destination_fails_closed(self):
        self.init();self.advance();self.accept_run(1)
        new_loop=self.root/'loop-2';new_loop.mkdir()
        (new_loop/'keep.txt').write_text('not ours\n')
        self.assertEqual(self.reason(self.redirect,new_loop,approval=self.redirect_approval()),
                         'continuation_conflict')
        self.assertEqual((new_loop/'keep.txt').read_text(),'not ours\n')
        self.assertFalse((self.loop/'redirect-receipt.json').exists())
        self.assertEqual(B.inspect(self.loop)['status'],'checkpoint_required')
        # A complete loop that is not this pending redirect's continuation is
        # neither adopted nor removed.
        (new_loop/'keep.txt').unlink();new_loop.rmdir()
        B.initialize(self.env2,self.reg,new_loop,evidence_mode='synthetic')
        before=self.tree(new_loop)
        self.assertEqual(self.reason(self.redirect,new_loop,approval=self.redirect_approval()),
                         'continuation_conflict')
        self.assertEqual(self.tree(new_loop),before)
        self.assertEqual([r['kind'] for r in self.records()],
                         ['genesis','iteration_started','iteration_recorded'])


class DestinationPublicationTests(LoopBase):
    """The destination's parent may be replaced after the pathname preflight."""

    def swap(self,base,target,*,rename_to=None,when=None):
        """Replace `base` with a symlink to `target`, once, after a real call.

        `when` is the pathname whose preflight triggers the swap; it defaults to
        `base` itself, and is set to a *descendant* of `base` to reproduce the
        swap of a non-final ancestor that `O_NOFOLLOW` on a single open cannot
        see.
        """
        real=B.no_links;done=[];trigger=Path(when) if when is not None else base
        def swapping(path):
            result=real(path)
            if Path(path)==trigger and not done:
                if rename_to is None:base.rmdir()
                else:os.rename(base,rename_to)
                base.symlink_to(target);done.append(True)
            return result
        return patch.object(B,'no_links',side_effect=swapping),done

    def attacker(self):
        base=self.root/'base';base.mkdir(mode=0o700)
        target=self.root/'attacker';target.mkdir(mode=0o700)
        return base,target

    def nested_attacker(self):
        """`base/mid` is the destination's parent; `base` is a *non-final* ancestor.

        The attacker tree mirrors the swapped-away component (`attacker/mid`), so
        every pathname below the swap resolves into it and an open of the final
        parent component alone succeeds.
        """
        base=self.root/'base';(base/'mid').mkdir(mode=0o700,parents=True)
        target=self.root/'attacker';(target/'mid').mkdir(mode=0o700,parents=True)
        return base,target

    def remnant(self,path):
        """Exactly the pre-publication set an interrupted redirect leaves: no seal."""
        path.mkdir(mode=0o700,parents=True)
        (path/'transitions').mkdir(mode=0o700);(path/'iterations').mkdir(mode=0o700)
        (path/'loop.json').write_bytes(canonical(self.env2)+b'\n')
        (path/'registry.json').write_bytes(canonical(self.reg)+b'\n')
        return path

    def test_parent_swapped_after_preflight_publishes_nothing(self):
        base,target=self.attacker();patched,done=self.swap(base,target)
        with patched:
            with self.assertRaises(ContractError):self.init(loop=base/'loop')
        self.assertEqual(done,[True])
        self.assertEqual(list(target.iterdir()),[])

    def test_publication_after_parent_swap_stays_in_the_verified_directory(self):
        base,target=self.attacker();moved=self.root/'moved'
        (target/'loop').mkdir(mode=0o700)      # ready to receive escaped bytes
        real=E.snapshot;calls=[]
        def swapping(root):
            calls.append(root)
            # Between the destination's creation and its publication point.
            if len(calls)==2:os.rename(base,moved);base.symlink_to(target)
            return real(root)
        with patch.object(B,'snapshot',side_effect=swapping):
            view=self.init(loop=base/'loop')
        self.assertEqual(view['status'],'ready')
        self.assertEqual(list((target/'loop').iterdir()),[])
        for name in ('loop.json','registry.json','loop-seal.json','loop-state.json',
                     'transitions/0000-genesis.json'):
            self.assertTrue((moved/'loop'/name).is_file(),name)
        self.assertEqual(B.inspect(moved/'loop')['status'],'ready')

    def test_continuation_parent_swap_publishes_nothing(self):
        self.init();self.advance();self.accept_run(1)
        approval=self.redirect_approval()
        base,target=self.attacker();patched,done=self.swap(base,target)
        with patched:
            with self.assertRaises(ContractError):self.redirect(base/'loop-2',approval=approval)
        self.assertEqual(done,[True])
        self.assertEqual(list(target.iterdir()),[])
        self.assertFalse((self.loop/'redirect-receipt.json').exists())
        self.assertEqual([r['kind'] for r in self.records()],
                         ['genesis','iteration_started','iteration_recorded'])
        self.assertEqual(B.inspect(self.loop)['status'],'checkpoint_required')

    def test_non_final_ancestor_swapped_after_preflight_publishes_nothing(self):
        # `O_NOFOLLOW` constrains only the *final* component of one open, so a
        # single open of the parent pathname still re-resolves every ancestor
        # the preflight proved. Here the swapped component is not the parent but
        # its parent, and `base/mid` is a real directory in the attacker's tree.
        base,target=self.nested_attacker();moved=self.root/'moved'
        parent=base/'mid'
        patched,done=self.swap(base,target,rename_to=moved,when=parent)
        with patched:
            reason=self.reason(self.init,loop=parent/'loop')
        self.assertEqual(done,[True])
        self.assertEqual(reason,'loop_parent_unavailable')
        # Zero bytes in the attacker's tree, and no loop published anywhere.
        self.assertEqual(self.tree(target),{})
        self.assertEqual(list((target/'mid').iterdir()),[])
        self.assertFalse((moved/'mid'/'loop').exists())
        self.assertEqual(self.tree(moved),{})

    def test_continuation_non_final_ancestor_swap_touches_nothing(self):
        self.init();self.advance();self.accept_run(1)
        approval=self.redirect_approval()
        base,target=self.nested_attacker();moved=self.root/'moved'
        destination=self.remnant(base/'mid'/'loop-2')
        # A byte-identical remnant in the attacker's tree: reached through the
        # swapped ancestor it satisfies every member check and is removed.
        planted=self.remnant(target/'mid'/'loop-2')
        before=self.tree(target);mine=self.tree(destination)
        patched,done=self.swap(base,target,rename_to=moved,when=destination.parent)
        with patched:
            self.assertEqual(self.reason(self.redirect,destination,approval=approval),
                             'continuation_conflict')
        self.assertEqual(done,[True])
        # Nothing written to and nothing removed from the attacker's tree.
        self.assertEqual(self.tree(target),before)
        self.assertTrue(planted.is_dir())
        self.assertEqual(self.tree(moved/'mid'/'loop-2'),mine)
        self.assertFalse((moved/'mid'/'loop-2'/'loop-seal.json').exists())
        self.assertFalse((self.loop/'redirect-receipt.json').exists())
        self.assertEqual([r['kind'] for r in self.records()],
                         ['genesis','iteration_started','iteration_recorded'])
        self.assertEqual(B.inspect(self.loop)['status'],'checkpoint_required')

    def test_missing_directory_primitive_fails_closed(self):
        with patch.object(B,'DIR_FD_READY',False):
            self.assertEqual(self.reason(self.init),'directory_descriptor_unavailable')
        self.assertFalse(self.loop.exists())

    # --- reconciliation of an already published continuation ------------------

    def lookalike(self,source,destination):
        """An attacker's copy of a published continuation at another pathname.

        Every seal field reconciliation checks is copied verbatim, so the copy
        satisfies all of them; only `workspace` — which nothing checks — differs,
        and the whole loop is rebuilt around the resulting seal hash so it loads
        cleanly. That hash is exactly what a pathname read would commit.
        """
        shutil.copytree(source,destination)
        seal=json.loads((destination/'loop-seal.json').read_bytes())
        seal['workspace']=str(self.root/'attacker-workspace')
        state=json.loads((destination/'loop-state.json').read_bytes())
        state['seal_hash']=digest(seal)
        genesis=json.loads((destination/'transitions/0000-genesis.json').read_bytes())
        genesis['seal_hash']=digest(seal);genesis['after_state_hash']=digest(state)
        for name,value in (('loop-seal.json',seal),('loop-state.json',state),
                           ('transitions/0000-genesis.json',genesis)):
            (destination/name).write_bytes(canonical(value)+b'\n')
        return digest(seal)

    def rename_swap(self,base,target,moved):
        """Swap a real directory into a non-final ancestor after the first anchor.

        A *rename* leaves no symlink anywhere, so `no_links` sees a canonical
        path and `O_NOFOLLOW` — which never constrained an ancestor — sees
        nothing either. The swap fires once, immediately after a descriptor has
        been anchored, which is the window every later pathname read sits in.
        """
        real=B._directory_fd;done=[]
        def anchoring(name,*,dir_fd=None,reason='loop_parent_unavailable'):
            fd=real(name,dir_fd=dir_fd,reason=reason)
            if not done:
                os.rename(base,moved);os.rename(target,base);done.append(True)
            return fd
        return patch.object(B,'_directory_fd',side_effect=anchoring),done

    def pending_continuation(self,approval):
        """Publish a continuation the way an interrupted redirect does, then plant.

        `base/mid/loop-2` is ours, published descriptor-relative by a redirect
        that crashed before its record — exactly what reconciliation adopts.
        `attacker/mid/loop-2` is the lookalike a swap of `base` rebinds that same
        pathname to.
        """
        base,target=self.nested_attacker();destination=base/'mid'/'loop-2'
        with self.crash_before('redirect'):
            with self.assertRaises(RuntimeError):self.redirect(destination,approval=approval)
        ours=digest(self.read('loop-seal.json',destination))
        theirs=self.lookalike(destination,target/'mid'/'loop-2')
        self.assertNotEqual(theirs,ours)
        return base,target,destination,ours,theirs

    def no_pathname_after_anchor(self):
        """Fail every pathname resolution made after the destination is anchored.

        A name resolved once the descriptor is held is resolved against whatever
        the tree holds *now* — exactly what a swapped ancestor rebinds — so the
        window between the anchor and the adopted view must contain none. Only
        `dir_fd`-relative calls, which are the anchored ones, stay real; the
        pathname work `_reconcile_continuation` does *before* the anchor is left
        alone, and so is everything the redirect does after it returns.
        """
        real=B._reconcile_continuation;real_fd=B._directory_fd
        real_stat=os.stat;real_lstat=os.lstat;anchored=[];tried=[]
        def guarded_stat(path,*args,dir_fd=None,**kw):
            if dir_fd is None and anchored:
                tried.append(path);raise AssertionError('pathname resolved after the anchor')
            return real_stat(path,*args,dir_fd=dir_fd,**kw)
        def guarded_lstat(path,*args,**kw):
            if anchored:
                tried.append(path);raise AssertionError('pathname resolved after the anchor')
            return real_lstat(path,*args,**kw)
        def anchoring(name,*,dir_fd=None,reason='loop_parent_unavailable'):
            fd=real_fd(name,dir_fd=dir_fd,reason=reason);anchored.append(name);return fd
        def reconciling(*args,**kw):
            anchored.clear()
            with patch.object(os,'stat',guarded_stat),patch.object(os,'lstat',guarded_lstat),\
                 patch.object(B,'_directory_fd',side_effect=anchoring):
                return real(*args,**kw)
        return patch.object(B,'_reconcile_continuation',new=reconciling),tried

    def test_published_continuation_ancestor_swap_after_anchor_binds_the_anchored_loop(self):
        # A rename of a non-final ancestor rebinds the destination pathname the
        # moment the descriptor is anchored. The descriptor is the authority:
        # the adopted bytes, and the hash the redirect commits, are the ones it
        # holds — never the lookalike the rebound pathname now names.
        self.init();self.advance();self.accept_run(1)
        approval=self.redirect_approval();moved=self.root/'moved'
        base,target,destination,ours,theirs=self.pending_continuation(approval)
        mine=self.tree(destination);planted=self.tree(target/'mid'/'loop-2')
        self.assertIn('loop-seal.json',mine)
        committed=[r['kind'] for r in self.records()]
        patched,done=self.rename_swap(base,target,moved)
        with patched:
            result=self.redirect(destination,approval=approval)
        self.assertEqual(done,[True])
        self.assertEqual(result['status'],'redirected')
        self.assertEqual(result['continuation_seal_hash'],ours)
        self.assertNotEqual(result['continuation_seal_hash'],theirs)
        record=self.records()[-1]
        self.assertEqual([r['kind'] for r in self.records()],committed+['redirect'])
        self.assertEqual((record['kind'],record['evidence']['continuation_hash']),('redirect',ours))
        # Neither tree is written to or removed: the lookalike the destination
        # pathname now names is untouched, and ours — reachable only under
        # `moved` since the swap — is still the one the crashed redirect made.
        self.assertEqual(self.tree(destination),planted)
        self.assertEqual(digest(self.read('loop-seal.json',destination)),theirs)
        self.assertEqual(self.tree(moved/'mid'/'loop-2'),mine)

    def test_reconciliation_resolves_no_pathname_after_the_anchor(self):
        # The retained descriptor carries the whole decision, so reconciliation
        # needs no pathname once it holds one: with every post-anchor resolution
        # made to fail, a foreign published loop is still refused and our own
        # published continuation is still adopted, on fd-bound bytes alone.
        self.init();self.advance();self.accept_run(1)
        approval=self.redirect_approval()
        foreign=self.root/'foreign';B.initialize(self.env2,self.reg,foreign,evidence_mode='synthetic')
        before=self.tree(foreign)
        guarded,tried=self.no_pathname_after_anchor()
        with guarded:
            self.assertEqual(self.reason(self.redirect,foreign,approval=approval),
                             'continuation_conflict')
        self.assertEqual(tried,[])
        self.assertEqual(self.tree(foreign),before)
        self.assertFalse((self.loop/'redirect-receipt.json').exists())
        self.assertEqual(B.inspect(self.loop)['status'],'checkpoint_required')
        base,target,destination,ours,theirs=self.pending_continuation(approval)
        mine=self.tree(destination)
        guarded,tried=self.no_pathname_after_anchor()
        with guarded:
            result=self.redirect(destination,approval=approval)
        self.assertEqual(tried,[])
        self.assertEqual(result['status'],'redirected')
        self.assertEqual(result['continuation_seal_hash'],ours)
        self.assertNotEqual(result['continuation_seal_hash'],theirs)
        record=self.records()[-1]
        self.assertEqual((record['kind'],record['evidence']['continuation_hash']),('redirect',ours))
        # The continuation is adopted byte-for-byte, and nobody else is touched.
        self.assertEqual(self.tree(destination),mine)
        self.assertEqual(digest(self.read('loop-seal.json',target/'mid'/'loop-2')),theirs)

    def test_anchored_load_is_the_pathname_load_without_any_pathname(self):
        # Every post-publication read of a continuation — seal, envelope,
        # registry, projection, ledger and the run state each recorded iteration
        # is reconciled against — goes through the anchored descriptor.
        self.init();self.advance()
        expected=B._load(self.loop)[1:]
        parent_fd=B._parent_fd(self.loop.parent)
        try:
            loop_fd=B._directory_fd(self.loop.name,dir_fd=parent_fd)
            try:
                with patch.object(B,'_under',side_effect=AssertionError('pathname read')),\
                     patch.object(B,'runtime',side_effect=AssertionError('pathname read')):
                    anchored=B._load_at(self.loop,loop_fd)
            finally:os.close(loop_fd)
        finally:os.close(parent_fd)
        self.assertEqual(anchored,expected)
        self.assertEqual(len(anchored[4]),3)

    def test_anchored_load_fails_closed_on_a_special_member(self):
        self.init()
        (self.loop/'loop-state.json').unlink();os.mkfifo(self.loop/'loop-state.json')
        parent_fd=B._parent_fd(self.loop.parent)
        try:
            loop_fd=B._directory_fd(self.loop.name,dir_fd=parent_fd)
            try:self.assertEqual(self.reason(B._load_at,self.loop,loop_fd),'sealed_loop_changed')
            finally:os.close(loop_fd)
        finally:os.close(parent_fd)


if __name__=='__main__':unittest.main()
