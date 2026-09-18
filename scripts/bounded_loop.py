"""Chain controller over `executor_runtime`. No second runtime, no global routing.

A loop is a hash-bound chain of bounded iterations over stages the parent has
already admitted. Everything inside an iteration is delegated verbatim to
`executor_runtime`; this module owns only the order of iterations, the parent
checkpoint between them, and the immutable continuation produced by a redirect.

Authority lives in `<loop_dir>/transitions/NNNN-<kind>.json`, written create-only
and never rewritten. `loop-state.json` is a projection with no independent
authority: a state-only edit fails before any action runs. Final acceptance stays
parent-only — every receipt and every ledger record carries parent_acceptance
false. This code is not an operating-system sandbox, and the ledger is not
evidence about the quality of any result.

Every artifact published before that commit point — a selection, checkpoint or
redirect receipt, and the continuation loop itself — may survive a crash that
never reached the record. The next invocation reconciles it instead of wedging:
it adopts the artifact only when the exact canonical bytes bind the pending
transition and the current ledger tip, and otherwise fails closed. Nothing is
re-asked and nothing is re-run; an unpublished continuation remnant is the only
thing ever removed, and only when every bounded member proves it.

Publication itself is descriptor-relative. `no_links` proves what a path
component was, not what it still is one syscall later, so the destination's
parent is resolved exactly once and the retained no-follow descriptor carries
the loop directory and its initial members — and, once published, every byte
reconciliation reads back out of it.

Lock discipline (exact): the only two targets are `<loop_dir>/loop-seal.json` and
`<workspace>/.staged-routing-workspace.json`, in that order and never nested.
`advance`, `checkpoint` and `accept` hold only the loop seal, because
`executor_runtime.initialize`/`advance` each take the workspace marker on a fresh
fd. `initialize` and `redirect` hold the marker and therefore call no
`executor_runtime` mutation path while holding it.
"""
from __future__ import annotations
import math
import os
from pathlib import Path
import re
import stat
import time
import executor_routes as routes
import executor_runtime as runtime
import loop_menu
from executor_runtime import snapshot
from runtime_support import StageError,create_file
from schema_validation import ContractError,canonical,digest,loads
from stage_contracts import clean_relative,contract,no_links,read_file,validate_stage,workspace

TERMINAL={'redirected','stopped','loop_accepted'}
STATUSES=TERMINAL|{'ready','iteration_running','checkpoint_required'}
KINDS=('genesis','iteration_started','iteration_recorded','interrupted','checkpoint','redirect','accept')
MAX_TRANSITIONS=32
EMPTY_EVIDENCE={'stage_id':None,'run_relative':None,'result_hash':None,'tree_hash':None,
                'accepted':False,'receipt_hash':None,'approval_hash':None,'continuation_hash':None}
EMPTY_DELTA={'calls':0,'input_tokens':0,'output_tokens':0}
MAX_MEMBER_BYTES=1024*1024
# The bounded pre-publication member set of a loop directory: everything
# `_initialize_unlocked` may have created before it writes `loop-seal.json`.
UNPUBLISHED_MEMBERS=('loop.json','registry.json')
UNPUBLISHED_DIRECTORIES=('transitions','iterations')
# Descriptor-relative creation is the only publication path. Where the platform
# cannot supply it, publication fails closed instead of falling back to
# pathnames, which a parent-directory swap can redirect after the preflight.
DIR_FD_READY=(hasattr(os,'O_DIRECTORY') and hasattr(os,'O_NOFOLLOW') and hasattr(os,'O_CLOEXEC') and
              {os.open,os.mkdir,os.unlink,os.rmdir,os.stat}<=os.supports_dir_fd)


# --- contracts ---------------------------------------------------------------

def validate_envelope(envelope,*,verify_files=True):
    """Refuse a loop the runtime could only reject later, one reason code each."""
    contract(envelope,'loop-envelope.schema.json')
    stages=envelope['stages']
    # Shape first, without touching the filesystem, so the cross-stage reasons
    # below are reported instead of an unrelated file-binding failure.
    for stage in stages:validate_stage(stage,verify_files=False)
    identities=[stage['stage_id'] for stage in stages]
    if len(set(identities))!=len(identities):raise ContractError('duplicate_stage_identity')
    if any(identity in loop_menu.CONTROLS for identity in identities):
        raise ContractError('reserved_stage_identity')
    for stage in stages:
        if stage['workspace']!=envelope['workspace']:raise ContractError('stage_workspace_mismatch')
        if stage['limits']['max_seconds']>envelope['max_seconds']:raise ContractError('stage_budget_exceeds_loop')
    # A later stage that pins a file an earlier stage may rewrite is dead on
    # arrival: initialize re-validates with verify_files=True at iteration N.
    for index,earlier in enumerate(stages):
        writable=set(earlier['allowed_paths'])
        for later in stages[index+1:]:
            if any(item['path'] in writable for item in later['inputs']):
                raise ContractError('stage_input_mutated_by_earlier_stage')
    if not verify_files:return
    for stage in stages:validate_stage(stage,verify_files=True)


def _create(path,value,reason='destination_exists_or_unwritable'):
    try:create_file(Path(path),canonical(value)+b'\n')
    except StageError:raise ContractError(reason) from None


def _under(loop,relative):
    clean_relative(relative);path=no_links(Path(loop)/relative)
    if not path.is_relative_to(Path(loop)):raise ContractError('path_escape')
    return path


# --- descriptor-relative publication -----------------------------------------
# A pathname preflight (`no_links`) proves what a component *was*. It cannot
# prove what it still is at `mkdir`/`open` time: the parent may be replaced by a
# symlink in between, and every later pathname would then resolve inside the
# attacker's tree. So the destination's parent is resolved exactly once, and the
# retained no-follow descriptor — not the pathname — carries the loop directory,
# its initial members and the bounded cleanup of an unpublished remnant.

def _dir_flags():
    return os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC


def _directory_fd(name,*,dir_fd=None,reason='loop_parent_unavailable'):
    """Open one already-anchored component, relative to a retained descriptor."""
    if not DIR_FD_READY:raise ContractError('directory_descriptor_unavailable')
    try:return os.open(name,_dir_flags(),dir_fd=dir_fd)
    except OSError:raise ContractError(reason) from None


def _parent_fd(path,*,reason='loop_parent_unavailable'):
    """Anchor an absolute directory pathname component by component from `/`.

    `O_NOFOLLOW` constrains only the *final* component of one `open`: every
    ancestor is still resolved by the kernel at open time. A single open of the
    parent pathname therefore re-resolves the whole prefix the preflight just
    proved, so replacing a higher ancestor in between redirects the descriptor —
    and every byte published through it — into the attacker's tree, while the
    final component alone is still a real directory there.

    Walking instead opens each child relative to the descriptor of the directory
    that was verified one syscall earlier, so no component is resolved by name
    twice and no ancestor is re-traversed. Only the last descriptor survives;
    every other one is closed exactly once on every branch.
    """
    if not DIR_FD_READY:raise ContractError('directory_descriptor_unavailable')
    parts=Path(path).parts
    if not parts or parts[0]!='/' or any(part in ('','.','..') for part in parts[1:]):
        raise ContractError('noncanonical_path')
    held=[]
    try:
        held.append(os.open('/',_dir_flags()))
        for part in parts[1:]:
            held.append(os.open(part,_dir_flags(),dir_fd=held[-1]))
            # O_DIRECTORY already refuses a non-directory; restating the type on
            # the descriptor keeps the boundary where the descriptor is retained.
            if not stat.S_ISDIR(os.fstat(held[-1]).st_mode):raise ContractError(reason)
    except OSError:
        for fd in held:os.close(fd)
        raise ContractError(reason) from None
    except BaseException:
        for fd in held:os.close(fd)
        raise
    for fd in held[:-1]:os.close(fd)
    return held[-1]


def _mkdir_at(dir_fd,name,reason='loop_destination_unwritable'):
    try:os.mkdir(name,0o700,dir_fd=dir_fd)
    except FileExistsError:raise ContractError('loop_dir_exists') from None
    except OSError:raise ContractError(reason) from None


def _create_at(dir_fd,name,value,reason='destination_exists_or_unwritable'):
    """Create-only, descriptor-relative twin of `runtime_support.create_file`."""
    data=canonical(value)+b'\n'
    try:fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=dir_fd)
    except OSError:raise ContractError(reason) from None
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
    except OSError:raise ContractError(reason) from None


def _member(dir_fd,name):
    try:return os.stat(name,dir_fd=dir_fd,follow_symlinks=False)
    except OSError:return None


def _read_at(dir_fd,name,limit=MAX_MEMBER_BYTES):
    """Bounded no-follow read of one regular member, or None.

    `O_NONBLOCK` keeps a planted fifo or device from stalling the open; the
    regular-file check on the descriptor then refuses it, so a special member
    fails closed instead of hanging the caller.
    """
    try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|getattr(os,'O_NONBLOCK',0),dir_fd=dir_fd)
    except OSError:return None
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>limit:return None
        try:return stream.read(limit+1)
        except OSError:return None


def _read_json_at(dir_fd,name,reason='sealed_loop_changed'):
    """Anchored twin of `runtime.read`: one bounded no-follow regular member."""
    raw=_read_at(dir_fd,name)
    if raw is None or len(raw)>MAX_MEMBER_BYTES:raise ContractError(reason)
    return loads(raw)


def _empty_directory(dir_fd,name):
    try:child=_directory_fd(name,dir_fd=dir_fd,reason='loop_destination_unwritable')
    except ContractError:return False
    try:return not os.listdir(child)
    finally:os.close(child)


def _discard_unpublished(parent_fd,name,loop_fd):
    """Remove the bounded pre-publication set, and only while unpublished.

    `loop-seal.json` is the publication point and the only thing a committed
    record can name (through its hash), so a directory that has one is never
    touched. Every removal is descriptor-relative and named explicitly: no
    recursive walk of a directory a swap may have moved underneath us.
    """
    if _member(loop_fd,'loop-seal.json') is not None:return False
    for member in UNPUBLISHED_MEMBERS:
        try:os.unlink(member,dir_fd=loop_fd)
        except OSError:pass
    for member in UNPUBLISHED_DIRECTORIES:
        try:os.rmdir(member,dir_fd=loop_fd)
        except OSError:pass
    try:os.rmdir(name,dir_fd=parent_fd)
    except OSError:return False
    return True


# --- bounded reconciliation of pre-commit artifacts ---------------------------
# Every artifact published before the commit point may survive a crash that
# never reached `transitions/NNNN-<kind>.json`. The next invocation adopts such
# an artifact only when its exact canonical bytes bind the pending transition it
# is about to commit *and* the current ledger tip; committing moves the tip, so
# the same bytes can never be adopted twice. Anything else fails closed, and no
# worker, deterministic action or provider call is ever replayed.

def _adoptable(path,value):
    try:return read_file(Path(path),MAX_MEMBER_BYTES)==canonical(value)+b'\n'
    except ContractError:return False


def _publish_receipt(path,receipt,reason):
    path=Path(path)
    try:create_file(path,canonical(receipt)+b'\n')
    except StageError:
        if not _adoptable(path,receipt):raise ContractError(reason) from None


def _selection_shape(receipt):
    """The three (source, reason) pairs `loop_menu.choose` can actually emit.

    A parent proposal never reaches a provider, so it must carry no answers, no
    usage and no model; a policy default is always the first control.
    """
    source=receipt['source'];reason=receipt['reason']
    if source=='parent':
        return (reason=='parent_proposal' and receipt['answers']=={} and
                receipt['confidence']=={'choice':0.0,'margin':0.0} and
                receipt['usage']=={'input_tokens':0,'output_tokens':0} and
                receipt['provider_model'] is None)
    if source=='policy':
        return reason=='parent_default' and receipt['decision']==loop_menu.CONTROLS[0]
    return (reason=='typed_recommendation' and isinstance(receipt['provider_model'],str)
            and receipt['provider_model']!='')


def _pending_selection(loop,seal,state,records,iteration,ctx,proposed):
    """The selection an interrupted `advance` already published, or None.

    Adopting it is what keeps reconciliation free of a second provider call: the
    receipt records the decision, the offered menu and the exact usage of the
    call that already happened.
    """
    path=loop/('selection-receipt-%d.json'%iteration)
    if not (path.exists() or path.is_symlink()):return None
    try:
        raw=read_file(path,MAX_MEMBER_BYTES);receipt=loads(raw)
        contract(receipt,'loop-receipt.schema.json')
    except ContractError:raise ContractError('selection_already_recorded') from None
    entries=state['iterations'];last=entries[-1] if entries else None
    allowed=loop_menu.allowed(ctx)
    if not _selection_shape(receipt):raise ContractError('selection_already_recorded')
    if (raw!=canonical(receipt)+b'\n' or receipt['kind']!='selection' or
            receipt['loop_hash']!=seal['loop_hash'] or receipt['loop_state_hash']!=digest(state) or
            receipt['transition_hash']!=digest(records[-1]) or receipt['iteration']!=iteration or
            receipt['allowed']!=allowed or receipt['decision'] not in allowed or
            receipt['evidence_hash']!=(last['result_hash'] if last else None) or
            receipt['carried_tree_hash'] is not None or receipt['parent_acceptance'] is not False or
            (proposed is not None and receipt['decision']!=proposed)):
        raise ContractError('selection_already_recorded')
    return receipt


# --- projection --------------------------------------------------------------

def _menu_context(envelope,state):
    entries=state['iterations'];last=entries[-1] if entries else None
    used={entry['stage_id'] for entry in entries}
    remaining=[stage['stage_id'] for stage in envelope['stages'] if stage['stage_id'] not in used]
    hard=any(routes.hard_owner(stage) for stage in envelope['stages']
             if stage['stage_id'] in remaining)
    return {'goal_hash':digest(envelope['goal']),'iteration':state['iteration'],
            'max_iterations':envelope['max_iterations'],
            'last_status':last['status'] if last else 'none',
            'last_accepted':bool(last['accepted']) if last else False,
            'scope_violations':[last['status']] if last and last['status']=='scope_ambiguity_returned_to_parent' else [],
            'remaining_stage_ids':remaining,'hard_owner_boundary':hard}


def _apply(state,record):
    """Pure and total: every changed field is derived from the record alone."""
    new=loads(canonical(state));evidence=record['evidence'];kind=record['kind']
    new.update(status=record['status'],reason=record['reason'],iteration=record['iteration'])
    if kind=='iteration_started':
        new['last_selection']={'iteration':record['iteration'],'stage_id':evidence['stage_id'],
                               'run_relative':evidence['run_relative'],'receipt_hash':evidence['receipt_hash']}
    elif kind=='iteration_recorded':
        entry={'index':record['iteration'],'stage_id':evidence['stage_id'],
               'run_relative':evidence['run_relative'],'result_hash':evidence['result_hash'],
               'tree_hash':evidence['tree_hash'],'status':record['reason'],
               'accepted':bool(evidence['accepted'])}
        new['iterations']=sorted([x for x in new['iterations'] if x['index']!=entry['index']]+[entry],
                                 key=lambda x:x['index'])
    elif kind=='checkpoint':
        new['last_checkpoint']={'iteration':record['iteration'],'decision':record['reason'],
                                'receipt_hash':evidence['receipt_hash'],'approval_hash':evidence['approval_hash']}
    elif kind=='redirect':
        new['redirected_to']={'iteration':record['iteration'],'continuation_hash':evidence['continuation_hash'],
                              'receipt_hash':evidence['receipt_hash'],'approval_hash':evidence['approval_hash']}
    elif kind=='accept':
        new['loop_approval']={'iteration':record['iteration'],'approval_hash':evidence['approval_hash'],
                              'loop_evidence_hash':evidence['result_hash']}
    if evidence['accepted'] and kind!='iteration_recorded':
        # A parent decision may record an acceptance the projection had not seen.
        for entry in new['iterations']:
            if entry['index']==record['iteration']:entry['accepted']=True
    delta=record['jev_delta']
    new['jev_calls']=new['jev_calls']+delta['calls']
    new['jev_usage']={key:new['jev_usage'][key]+delta[key] for key in ('input_tokens','output_tokens')}
    return new


def _record(records,state,kind,*,status,reason,iteration,jev_delta=None,**evidence):
    if kind not in KINDS:raise ContractError('transition_kind')
    if len(records)>=MAX_TRANSITIONS:raise ContractError('transition_budget')
    unknown=set(evidence)-set(EMPTY_EVIDENCE)
    if unknown:raise ContractError('transition_evidence_fields')
    record={'schema_version':1,'sequence':len(records),'kind':kind,
            'previous_transition_hash':digest(records[-1]) if records else None,
            'seal_hash':state['seal_hash'],
            'before_state_hash':digest(state) if records else None,
            'after_state_hash':None,'status':status,'reason':reason,'iteration':iteration,
            'evidence':{**EMPTY_EVIDENCE,**evidence},'jev_delta':dict(jev_delta or EMPTY_DELTA),
            'recorded_at':time.time(),'parent_acceptance':False}
    # _apply never reads after_state_hash, so the placeholder cannot feed back.
    candidate=_apply(state,record)
    record['after_state_hash']=digest(candidate)
    contract(record,'loop-transition.schema.json')
    return loads(canonical(record)),candidate


def _commit(loop,records,state,kind,**fields):
    """Create-only record (the commit point), then republish the projection."""
    record,candidate=_record(records,state,kind,**fields)
    name='%04d-%s.json'%(record['sequence'],record['kind'])
    _create(loop/'transitions'/name,record,'transition_already_recorded')
    runtime.save(loop/'loop-state.json',candidate)
    return candidate,records+[record]


def _public(loop,envelope,seal,state,records):
    return {'status':state['status'],'reason':state['reason'],'loop_id':envelope['loop_id'],
            'loop_directory':str(loop),'iteration':state['iteration'],
            'max_iterations':envelope['max_iterations'],'evidence_kind':seal['evidence_mode'],
            'seal_hash':state['seal_hash'],'loop_hash':seal['loop_hash'],
            'iterations':loads(canonical(state['iterations'])),
            'allowed':loop_menu.allowed(_menu_context(envelope,state)),
            'jev_calls':state['jev_calls'],'jev_usage':loads(canonical(state['jev_usage'])),
            'last_selection':loads(canonical(state['last_selection'])),
            'last_checkpoint':loads(canonical(state['last_checkpoint'])),
            'redirected_to':loads(canonical(state['redirected_to'])),
            'loop_approval':loads(canonical(state['loop_approval'])),
            'continuation':loads(canonical(seal['continuation'])),
            'transitions':len(records),'transition_hash':digest(records[-1]),
            'live_readiness_claim':False,'parent_acceptance':False}


# --- load --------------------------------------------------------------------

def _chain_of(names,read_one,seal):
    """The ledger rules, over whichever reader produced the member names.

    Shared verbatim by the pathname reader and the anchored one so a
    continuation is never held to a weaker chain than a public `inspect`.
    """
    if not 1<=len(names)<=MAX_TRANSITIONS:raise ContractError('transition_ledger_gap')
    records=[]
    for index,name in enumerate(names):
        try:
            record=read_one(name);contract(record,'loop-transition.schema.json')
        except ContractError:raise ContractError('transition_ledger_shape') from None
        if name!='%04d-%s.json'%(record['sequence'],record['kind']) or record['sequence']!=index:
            raise ContractError('transition_ledger_gap')
        records.append(record)
    first=records[0]
    if (first['kind']!='genesis' or first['sequence']!=0 or first['previous_transition_hash'] is not None
            or first['before_state_hash'] is not None or first['seal_hash']!=digest(seal)):
        raise ContractError('transition_genesis_mismatch')
    for index in range(1,len(records)):
        record=records[index]
        if (record['previous_transition_hash']!=digest(records[index-1]) or
                record['before_state_hash']!=records[index-1]['after_state_hash'] or
                record['seal_hash']!=digest(seal)):
            raise ContractError('transition_chain_broken')
    return records


def _chain(loop,seal):
    directory=loop/'transitions'
    try:names=sorted(path.name for path in directory.iterdir())
    except OSError:raise ContractError('transition_ledger_gap') from None
    return _chain_of(names,lambda name:runtime.read(directory/name),seal)


def _chain_at(loop_fd,seal):
    directory=_directory_fd('transitions',dir_fd=loop_fd,reason='transition_ledger_gap')
    try:
        names=sorted(os.listdir(directory))
        return _chain_of(names,lambda name:_read_json_at(directory,name,'transition_ledger_shape'),seal)
    except OSError:raise ContractError('transition_ledger_gap') from None
    finally:os.close(directory)


def _sealed(loop_dir):
    loop=no_links(Path(loop_dir).absolute())
    envelope=runtime.read(loop/'loop.json');registry=runtime.read(loop/'registry.json')
    seal=runtime.read(loop/'loop-seal.json');state=runtime.read(loop/'loop-state.json')
    if (seal.get('loop_hash')!=digest(envelope) or seal.get('registry_hash')!=digest(registry) or
            state.get('seal_hash')!=digest(seal)):
        raise ContractError('sealed_loop_changed')
    return loop,envelope,registry,seal,state,_chain(loop,seal)


def _run_state_at(loop_fd,relative):
    """Anchored walk of one bounded run path, child by child from the loop fd.

    `_under`'s `no_links` preflight proves the components it saw; walking from
    the retained descriptor instead means no component below the loop directory
    is ever resolved by name through a prefix somebody else may have rebound.
    """
    parts=clean_relative(relative).parts;held=[]
    try:
        for part in parts:
            held.append(_directory_fd(part,dir_fd=held[-1] if held else loop_fd,
                                      reason='iteration_evidence_mismatch'))
        return _read_json_at(held[-1],'state.json','iteration_evidence_mismatch')
    finally:
        for fd in held:os.close(fd)


def _reconcile(loop,envelope,state,records,*,read_run=None):
    """Bind the projection to the ledger and to persisted run state only.

    The mutable live workspace is deliberately not inspected here: later
    iterations are allowed to change it, and the parent is allowed to accept a
    run out of band before `checkpoint()` records that fact.

    `read_run` is how one recorded iteration's run state is fetched; the
    anchored caller supplies the descriptor-relative reader.
    """
    if read_run is None:read_run=lambda relative:runtime.read(_under(loop,relative)/'state.json')
    recorded=[record for record in records if record['kind']=='iteration_recorded']
    entries=state['iterations']
    if len(recorded)!=len(entries):raise ContractError('iteration_evidence_mismatch')
    for entry,record in zip(entries,recorded):
        evidence=record['evidence']
        if (entry['index']!=record['iteration'] or entry['stage_id']!=evidence['stage_id'] or
                entry['run_relative']!=evidence['run_relative'] or
                entry['result_hash']!=evidence['result_hash'] or
                entry['tree_hash']!=evidence['tree_hash']):
            raise ContractError('iteration_evidence_mismatch')
        try:run_state=read_run(entry['run_relative'])
        except ContractError:raise ContractError('iteration_evidence_mismatch') from None
        if run_state.get('result_hash')!=entry['result_hash']:
            raise ContractError('iteration_evidence_mismatch')
        # Acceptance is monotone, never strict equality.
        if entry['accepted'] and run_state.get('parent_accepted') is not True:
            raise ContractError('iteration_evidence_mismatch')
    if state['status'] not in STATUSES:raise ContractError('loop_state_status')
    if type(state['iteration']) is not int or not 0<=state['iteration']<=envelope['max_iterations']:
        raise ContractError('loop_iteration_range')
    if type(state['deadline']) not in (int,float) or not math.isfinite(state['deadline']):
        raise ContractError('loop_state_deadline')
    if (state['status']=='loop_accepted')!=bool(state['loop_approval']):
        raise ContractError('loop_state_acceptance')


def _load(loop_dir):
    loop,envelope,registry,seal,state,records=_sealed(loop_dir)
    if digest(state)!=records[-1]['after_state_hash']:raise ContractError('loop_projection_stale')
    _reconcile(loop,envelope,state,records)
    return loop,envelope,registry,seal,state,records


def _sealed_at(loop_fd):
    """`_sealed` through a retained descriptor. `loop` is a label, never a read."""
    envelope=_read_json_at(loop_fd,'loop.json');registry=_read_json_at(loop_fd,'registry.json')
    seal=_read_json_at(loop_fd,'loop-seal.json');state=_read_json_at(loop_fd,'loop-state.json')
    if (seal.get('loop_hash')!=digest(envelope) or seal.get('registry_hash')!=digest(registry) or
            state.get('seal_hash')!=digest(seal)):
        raise ContractError('sealed_loop_changed')
    return envelope,registry,seal,state,_chain_at(loop_fd,seal)


def _load_at(loop,loop_fd):
    """`_load` with every byte read through `loop_fd`. Same validators, same reasons."""
    envelope,registry,seal,state,records=_sealed_at(loop_fd)
    if digest(state)!=records[-1]['after_state_hash']:raise ContractError('loop_projection_stale')
    _reconcile(loop,envelope,state,records,read_run=lambda relative:_run_state_at(loop_fd,relative))
    return envelope,registry,seal,state,records


def inspect(loop_dir):
    loop,envelope,registry,seal,state,records=_load(loop_dir)
    return _public(loop,envelope,seal,state,records)


# --- initialize --------------------------------------------------------------

def _initialize_unlocked(envelope,registry,loop_dir,*,evidence_mode,gate_evidence,
                         continuation,expected_initial_tree_hash):
    """Assumes the caller already holds the workspace marker; acquires nothing."""
    envelope=loads(canonical(envelope));registry=loads(canonical(registry))
    validate_envelope(envelope);routes.validate_registry(registry)
    if evidence_mode not in ('synthetic','live'):raise ContractError('evidence_mode')
    if continuation is not None and set(continuation)!={
            'schema_version','source_loop_hash','source_loop_state_hash','source_transition_hash',
            'source_iteration','carried_result_hash','carried_tree_hash','reviewer','approved_by'}:
        raise ContractError('continuation_shape')
    root=workspace(envelope['workspace'])
    observed=snapshot(root)                                        # (i) pre-snapshot
    if expected_initial_tree_hash is not None and digest(observed)!=expected_initial_tree_hash:
        raise ContractError('continuation_tree_drift')
    loop=Path(loop_dir).absolute()
    if loop.exists() or loop.is_symlink():raise ContractError('loop_dir_exists')
    no_links(loop.parent)
    if loop.is_relative_to(root) or root.is_relative_to(loop):raise ContractError('loop_inside_workspace')
    if '.hermes' in loop.parts and loop.parts[loop.parts.index('.hermes')+1:loop.parts.index('.hermes')+2]!=('workspaces',):
        raise ContractError('active_profile_refused')
    # The parent pathname is anchored once, here, component by component;
    # nothing below re-resolves any of it by name.
    parent_fd=_parent_fd(loop.parent);loop_fd=None
    try:
        _mkdir_at(parent_fd,loop.name)
        loop_fd=_directory_fd(loop.name,dir_fd=parent_fd,reason='loop_destination_unwritable')
        _mkdir_at(loop_fd,'transitions');_mkdir_at(loop_fd,'iterations')
        _create_at(loop_fd,'loop.json',envelope);_create_at(loop_fd,'registry.json',registry)
        observed2=snapshot(root)                                   # (ii) pre-publication
        if digest(observed2)!=digest(observed):raise ContractError('continuation_tree_drift')
        if expected_initial_tree_hash is not None and digest(observed2)!=expected_initial_tree_hash:
            raise ContractError('continuation_tree_drift')
        seal={'schema_version':1,'loop_hash':digest(envelope),'registry_hash':digest(registry),
              'workspace':envelope['workspace'],'initial_tree_hash':digest(observed2),
              'evidence_mode':evidence_mode,'gate_evidence':gate_evidence,'continuation':continuation}
        _create_at(loop_fd,'loop-seal.json',seal)                  # publication point
        now=time.time()
        state={'schema_version':1,'seal_hash':digest(seal),'status':'ready','reason':'initialized',
               'iteration':0,'created_at':now,'deadline':now+envelope['max_seconds'],'iterations':[],
               'jev_calls':0,'jev_usage':{'input_tokens':0,'output_tokens':0},
               'last_selection':None,'last_checkpoint':None,'redirected_to':None,'loop_approval':None}
        genesis,candidate=_record([],state,'genesis',status='ready',reason='initialized',iteration=0)
        if digest(candidate)!=digest(state):raise ContractError('genesis_not_identity')
        transitions_fd=_directory_fd('transitions',dir_fd=loop_fd,reason='loop_destination_unwritable')
        try:_create_at(transitions_fd,'0000-genesis.json',genesis)
        finally:os.close(transitions_fd)
        # The projection is create-only here; only later commits republish it.
        _create_at(loop_fd,'loop-state.json',state)
    except BaseException:
        # An aborted initialize publishes no loop at all.
        if loop_fd is not None:_discard_unpublished(parent_fd,loop.name,loop_fd)
        raise
    finally:
        if loop_fd is not None:os.close(loop_fd)
        os.close(parent_fd)
    return _public(loop,envelope,seal,state,[genesis])


def _reconcile_continuation(destination,envelope,registry,*,evidence_mode,gate_evidence,
                            continuation,expected_initial_tree_hash):
    """Reconcile whatever an interrupted redirect left at the destination.

    Returns the adopted continuation view, or None when a remnant was removed
    and the caller may publish. A *published* continuation is adopted only when
    its seal carries exactly this pending redirect's continuation record — which
    binds the source's state hash, ledger tip and carried evidence — and its own
    ledger is still nothing but genesis. An *unpublished* remnant is removed
    only when every bounded member is byte-identical to what this call would
    have written; no seal means no committed record can name it. Anything else
    is somebody else's directory: `continuation_conflict`, nothing deleted.
    """
    if destination.is_symlink() or not destination.is_dir():raise ContractError('continuation_conflict')
    expected={'loop.json':canonical(loads(canonical(envelope)))+b'\n',
              'registry.json':canonical(loads(canonical(registry)))+b'\n'}
    parent_fd=_parent_fd(no_links(destination.parent),reason='continuation_conflict')
    try:
        loop_fd=_directory_fd(destination.name,dir_fd=parent_fd,reason='continuation_conflict')
        try:
            owned=all(_read_at(loop_fd,name)==data for name,data in expected.items())
            if _member(loop_fd,'loop-seal.json') is not None:
                # Every byte below comes off `loop_fd`. Re-reading `destination`
                # here would re-resolve the whole prefix the walk above already
                # anchored, and a non-final ancestor swapped in between would
                # bind this redirect to whatever loop is planted at the rebound
                # pathname — its seal hash is what the record would commit.
                try:
                    envelope_at,_,seal,state,records=_load_at(destination,loop_fd)
                    view=_public(destination,envelope_at,seal,state,records)
                except ContractError:raise ContractError('continuation_conflict') from None
                if not (owned and seal.get('continuation')==continuation and
                        seal.get('evidence_mode')==evidence_mode and
                        seal.get('gate_evidence')==gate_evidence and
                        seal.get('initial_tree_hash')==expected_initial_tree_hash and
                        seal.get('loop_hash')==digest(envelope) and
                        seal.get('registry_hash')==digest(registry) and
                        view['transitions']==1 and view['status']=='ready' and
                        view['iteration']==0 and view['iterations']==[]):
                    raise ContractError('continuation_conflict')
                return view
            if (set(os.listdir(loop_fd))!=set(UNPUBLISHED_MEMBERS+UNPUBLISHED_DIRECTORIES) or
                    not owned or any(not _empty_directory(loop_fd,name)
                                     for name in UNPUBLISHED_DIRECTORIES)):
                raise ContractError('continuation_conflict')
            if not _discard_unpublished(parent_fd,destination.name,loop_fd):
                raise ContractError('continuation_conflict')
            return None
        finally:os.close(loop_fd)
    finally:os.close(parent_fd)


def _publish_continuation(envelope,registry,new_loop_dir,*,evidence_mode,gate_evidence,
                          continuation,expected_initial_tree_hash):
    destination=Path(new_loop_dir).absolute()
    if destination.exists() or destination.is_symlink():
        adopted=_reconcile_continuation(destination,envelope,registry,evidence_mode=evidence_mode,
            gate_evidence=gate_evidence,continuation=continuation,
            expected_initial_tree_hash=expected_initial_tree_hash)
        if adopted is not None:return adopted
    return _initialize_unlocked(envelope,registry,destination,evidence_mode=evidence_mode,
        gate_evidence=gate_evidence,continuation=continuation,
        expected_initial_tree_hash=expected_initial_tree_hash)


def initialize(envelope,registry,loop_dir,*,evidence_mode,gate_evidence=None,
               continuation=None,expected_initial_tree_hash=None):
    validate_envelope(envelope)  # cheap unlocked fail-fast; re-checked under the marker
    root=workspace(envelope['workspace'])
    with runtime.lock(root/'.staged-routing-workspace.json'):
        return _initialize_unlocked(envelope,registry,loop_dir,evidence_mode=evidence_mode,
            gate_evidence=gate_evidence,continuation=continuation,
            expected_initial_tree_hash=expected_initial_tree_hash)


# --- one bounded iteration ---------------------------------------------------

def _deadline(state):
    if state['deadline']-time.time()<=0:raise ContractError('loop_deadline_exceeded')


def _classification(envelope):
    values={stage['data_policy']['classification'] for stage in envelope['stages']}
    # Disagreement fails closed: 'private' is refused by the outbound boundary.
    return values.pop() if len(values)==1 else 'private'


def _receipt(kind,seal,state,records,iteration,*,decision,allowed,source,reason,
             selection=None,evidence_hash=None,carried_tree_hash=None):
    receipt={'schema_version':1,'kind':kind,'loop_hash':seal['loop_hash'],
             'loop_state_hash':digest(state),'transition_hash':digest(records[-1]),
             'iteration':iteration,'allowed':list(allowed),'decision':decision,'source':source,
             'reason':reason,'answers':{},'confidence':{'choice':0.0,'margin':0.0},
             'evidence_hash':evidence_hash,'carried_tree_hash':carried_tree_hash,
             'usage':{'input_tokens':0,'output_tokens':0},'provider_model':None,
             'parent_acceptance':False}
    if selection is not None:
        receipt.update(answers=selection['answers'],confidence=selection['confidence'],
                       usage=selection['usage'],provider_model=selection['provider_model'])
    contract(receipt,'loop-receipt.schema.json')
    return loads(canonical(receipt))


def advance(loop_dir,*,fixed_stage=None,fixed_route=None,jev=None,providers=None,admissions=None):
    """Select one pre-admitted stage and delegate the whole iteration verbatim.

    Holds only `loop-seal.json`: `executor_runtime.initialize`/`advance` each take
    the workspace marker on a fresh fd, and a nested acquisition in this process
    would fail `one_writer_lock_busy`.
    """
    loop,envelope,registry,seal,state,records=_load(loop_dir)
    with runtime.lock(loop/'loop-seal.json'):
        loop,envelope,registry,seal,state,records=_load(loop)
        if state['status'] in TERMINAL:raise ContractError('loop_terminal')
        if state['status']=='checkpoint_required':raise ContractError('checkpoint_required_before_advance')
        if state['status']=='iteration_running':
            # Write-ahead status is never auto-resumed; a crash is not permission.
            state,records=_commit(loop,records,state,'interrupted',status='checkpoint_required',
                reason='interrupted_iteration_no_implicit_retry',iteration=state['iteration'])
            return _public(loop,envelope,seal,state,records)
        _deadline(state)
        if state['iteration']>=envelope['max_iterations']:raise ContractError('loop_iteration_budget')
        ctx=_menu_context(envelope,state)
        iteration=state['iteration']+1
        pending=_pending_selection(loop,seal,state,records,iteration,ctx,fixed_stage)
        if pending is None:
            selection=loop_menu.choose(ctx,proposed=fixed_stage,jev=jev,
                admission=(admissions or {}).get('stage_transition'),
                classification=_classification(envelope),documents=[{'loop_context':ctx}])
        else:
            # A published selection is the decision; re-asking would be a second
            # send, and a second answer would not be the one already on disk.
            selection={key:pending[key] for key in
                       ('decision','allowed','source','reason','answers','confidence',
                        'usage','provider_model')}
        entries=state['iterations'];last=entries[-1] if entries else None
        evidence_hash=last['result_hash'] if last else None
        delta={'calls':1 if selection['provider_model'] else 0,
               **{key:selection['usage'][key] for key in ('input_tokens','output_tokens')}}
        if selection['decision'] in loop_menu.CONTROLS:
            # No stage may be selected. The receipt records the selection that did
            # not start an iteration; the parent decides, the loop does not.
            receipt=_receipt('selection',seal,state,records,iteration,
                decision=selection['decision'],allowed=selection['allowed'],
                source=selection['source'],reason=selection['reason'],selection=selection,
                evidence_hash=evidence_hash)
            _publish_receipt(loop/('selection-receipt-%d.json'%iteration),receipt,
                             'selection_already_recorded')
            state,records=_commit(loop,records,state,'checkpoint',status='checkpoint_required',
                reason='scope_ambiguity_returned_to_parent' if ctx['scope_violations'] else 'no_admissible_stage',
                iteration=state['iteration'],receipt_hash=digest(receipt),jev_delta=delta)
            return _public(loop,envelope,seal,state,records)
        stage=next(item for item in envelope['stages'] if item['stage_id']==selection['decision'])
        run_relative='iterations/%d/run'%iteration
        receipt=_receipt('selection',seal,state,records,iteration,decision=selection['decision'],
            allowed=selection['allowed'],source=selection['source'],reason=selection['reason'],
            selection=selection,evidence_hash=evidence_hash)
        _publish_receipt(loop/('selection-receipt-%d.json'%iteration),receipt,
                         'selection_already_recorded')
        state,records=_commit(loop,records,state,'iteration_started',status='iteration_running',
            reason='iteration_started',iteration=iteration,stage_id=stage['stage_id'],
            run_relative=run_relative,receipt_hash=digest(receipt),jev_delta=delta)
        try:(loop/'iterations'/str(iteration)).mkdir(mode=0o700)
        except OSError:raise ContractError('iteration_directory_exists') from None
        run=_under(loop,run_relative)
        # The run directory is owned entirely by executor_runtime, unchanged.
        runtime.initialize(stage,registry,run,evidence_mode=seal['evidence_mode'],
                           gate_evidence=seal['gate_evidence'])
        result=runtime.advance(run,fixed_route=fixed_route,jev=jev,providers=providers,
                               admissions=admissions)
        run_state=runtime.read(run/'state.json');last_result=result['last_result']
        tree=last_result['final_tree_hash'] if last_result else digest(snapshot(seal['workspace']))
        if result['status']=='ready_for_parent_review':reason='iteration_ready_for_parent_review'
        elif last_result and last_result['scope_violations']:reason='scope_ambiguity_returned_to_parent'
        else:reason='iteration_needs_review'
        state,records=_commit(loop,records,state,'iteration_recorded',status='checkpoint_required',
            reason=reason,iteration=iteration,stage_id=stage['stage_id'],run_relative=run_relative,
            result_hash=run_state['result_hash'],tree_hash=tree,
            accepted=bool(result['parent_accepted']))
        return _public(loop,envelope,seal,state,records)


# --- recovery ----------------------------------------------------------------

def _recovered(loop,status,state,records):
    return {'status':status,'loop_directory':str(loop),'loop_status':state['status'],
            'reason':state['reason'],'iteration':state['iteration'],'transitions':len(records),
            'transition_hash':digest(records[-1]),'live_readiness_claim':False,
            'parent_acceptance':False}


def recover(loop_dir):
    """Republish a projection the ledger already commits; never re-execute.

    No worker, no deterministic action and no provider call is reachable from
    here: only the last committed record may be replayed, once.
    """
    loop=no_links(Path(loop_dir).absolute())
    with runtime.lock(loop/'loop-seal.json'):
        loop,envelope,registry,seal,state,records=_sealed(loop)
        tip=records[-1]
        if digest(state)==tip['after_state_hash']:
            _reconcile(loop,envelope,state,records)
            return _recovered(loop,'projection_current',state,records)
        if tip['before_state_hash'] is not None and digest(state)==tip['before_state_hash']:
            candidate=_apply(state,tip)
            if digest(candidate)!=tip['after_state_hash']:
                raise ContractError('loop_projection_unrecoverable')
            _reconcile(loop,envelope,candidate,records)
            runtime.save(loop/'loop-state.json',candidate)
            return _recovered(loop,'projection_replayed',candidate,records)
        raise ContractError('loop_projection_unrecoverable')


# --- parent decisions --------------------------------------------------------

def _is_sha256(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def _approval(approval,decision,keys,seal):
    if (not isinstance(approval,dict) or set(approval)!=keys or approval['decision']!=decision or
            approval['evidence_kind']!=seal['evidence_mode'] or
            not isinstance(approval['reviewer'],str) or not 1<=len(approval['reviewer'])<=128):
        raise ContractError('loop_approval_binding')


def _at_checkpoint(state):
    if state['status'] in TERMINAL:raise ContractError('loop_terminal')
    if state['status']!='checkpoint_required':raise ContractError('checkpoint_not_required')
    entries=state['iterations'];entry=entries[-1] if entries else None
    return entry if entry and entry['index']==state['iteration'] else None


def checkpoint(loop_dir,decision,*,approval=None):
    """The parent checkpoint between two iterations. Holds only the loop seal."""
    if decision not in ('continue','stop'):raise ContractError('checkpoint_decision')
    loop,envelope,registry,seal,state,records=_load(loop_dir)
    with runtime.lock(loop/'loop-seal.json'):
        loop,envelope,registry,seal,state,records=_load(loop)
        entry=_at_checkpoint(state)
        accepted=False;approval_hash=None
        if decision=='continue':
            if entry is None:raise ContractError('continue_requires_accepted_evidence')
            _approval(approval,'continue',{'decision','evidence_hash','reviewer','evidence_kind'},seal)
            if approval['evidence_hash']!=entry['result_hash']:raise ContractError('loop_approval_binding')
            run_state=runtime.read(_under(loop,entry['run_relative'])/'state.json')
            if (run_state.get('parent_accepted') is not True or
                    run_state.get('result_hash')!=entry['result_hash']):
                raise ContractError('continue_requires_accepted_evidence')
            status='ready';reason='parent_checkpoint_continue'
            accepted=True;approval_hash=digest(approval)
        else:
            if approval is not None:raise ContractError('loop_approval_binding')
            status='stopped';reason='parent_checkpoint_stop'
        receipt=_receipt('checkpoint',seal,state,records,state['iteration'],decision=decision,
            allowed=['continue','stop'],source='parent',reason=reason,
            evidence_hash=entry['result_hash'] if entry else None)
        _publish_receipt(loop/('checkpoint-receipt-%d.json'%state['iteration']),receipt,
                         'checkpoint_already_recorded')
        state,records=_commit(loop,records,state,'checkpoint',status=status,reason=reason,
            iteration=state['iteration'],
            stage_id=entry['stage_id'] if entry else None,
            run_relative=entry['run_relative'] if entry else None,
            result_hash=entry['result_hash'] if entry else None,
            tree_hash=entry['tree_hash'] if entry else None,
            accepted=accepted,receipt_hash=digest(receipt),approval_hash=approval_hash)
        return _public(loop,envelope,seal,state,records)


def redirect(loop_dir,new_loop_dir,envelope,registry,*,approval,evidence_mode,gate_evidence=None):
    """Publish an immutable continuation bound by hash to accepted evidence.

    The source's immutable set is never rewritten: exactly two paths are new
    (`redirect-receipt.json`, `transitions/<next>-redirect.json`) and exactly one
    is modified (`loop-state.json`). The workspace marker is held once, across
    verification and publication, so no admitted writer can move the tree in
    between; `executor_runtime.inspect` takes no lock and is the only runtime
    call made inside that section.
    """
    loop,source,source_registry,seal,state,records=_load(loop_dir)
    with runtime.lock(loop/'loop-seal.json'):
        loop,source,source_registry,seal,state,records=_load(loop)
        entry=_at_checkpoint(state)
        if entry is None:raise ContractError('redirect_requires_accepted_evidence')
        _approval(approval,'redirect',
                  {'decision','evidence_hash','carried_tree_hash','reviewer','evidence_kind'},seal)
        if (approval['evidence_hash']!=entry['result_hash'] or
                not _is_sha256(approval['carried_tree_hash'])):
            raise ContractError('loop_approval_binding')
        receipt_path=loop/'redirect-receipt.json'
        receipt=_receipt('redirect',seal,state,records,entry['index'],decision='redirect',
            allowed=['redirect'],source='parent',reason='parent_redirect',
            evidence_hash=entry['result_hash'],carried_tree_hash=approval['carried_tree_hash'])
        # A receipt an interrupted redirect published is adoptable only when it
        # is exactly this one, and that is settled here — before a continuation
        # could be published for a redirect this call does not own.
        if (receipt_path.exists() or receipt_path.is_symlink()) and not _adoptable(receipt_path,receipt):
            raise ContractError('redirect_already_recorded')
        root=workspace(seal['workspace'])
        with runtime.lock(root/'.staged-routing-workspace.json'):
            # 1. Workspace drift has one stable public reason, checked first.
            if (digest(snapshot(root))!=approval['carried_tree_hash'] or
                    approval['carried_tree_hash']!=entry['tree_hash']):
                raise ContractError('redirect_workspace_changed')
            # 2. Only hash-bound verified progress may carry.
            view=runtime.inspect(_under(loop,entry['run_relative']))
            if view['parent_accepted'] is not True or view['result_hash']!=entry['result_hash']:
                raise ContractError('redirect_requires_accepted_evidence')
            # 3. The goal and the stage menu may change; the workspace may not.
            if not isinstance(envelope,dict) or envelope.get('workspace')!=seal['workspace']:
                raise ContractError('continuation_workspace_mismatch')
            validate_envelope(envelope)
            continuation={'schema_version':1,'source_loop_hash':seal['loop_hash'],
                'source_loop_state_hash':digest(state),'source_transition_hash':digest(records[-1]),
                'source_iteration':entry['index'],'carried_result_hash':entry['result_hash'],
                'carried_tree_hash':approval['carried_tree_hash'],'reviewer':approval['reviewer'],
                'approved_by':'parent'}
            published=_publish_continuation(envelope,registry,new_loop_dir,evidence_mode=evidence_mode,
                gate_evidence=gate_evidence,continuation=continuation,
                expected_initial_tree_hash=approval['carried_tree_hash'])
            _publish_receipt(receipt_path,receipt,'redirect_already_recorded')
            state,records=_commit(loop,records,state,'redirect',status='redirected',
                reason='parent_redirect',iteration=state['iteration'],stage_id=entry['stage_id'],
                run_relative=entry['run_relative'],result_hash=entry['result_hash'],
                tree_hash=entry['tree_hash'],accepted=True,receipt_hash=digest(receipt),
                approval_hash=digest(approval),continuation_hash=published['seal_hash'])
        return {**_public(loop,source,seal,state,records),
                'continuation_loop':published['loop_directory'],
                'continuation_seal_hash':published['seal_hash']}


def accept(loop_dir,approval):
    """Parent-only final acceptance of the whole loop, bound to its evidence."""
    loop,envelope,registry,seal,state,records=_load(loop_dir)
    with runtime.lock(loop/'loop-seal.json'):
        loop,envelope,registry,seal,state,records=_load(loop)
        _at_checkpoint(state)
        _approval(approval,'accept',
                  {'decision','loop_evidence_hash','reviewer','evidence_kind'},seal)
        if approval['loop_evidence_hash']!=digest(state['iterations']):
            raise ContractError('loop_acceptance_binding')
        state,records=_commit(loop,records,state,'accept',status='loop_accepted',
            reason='explicit_parent_acceptance',iteration=state['iteration'],
            result_hash=approval['loop_evidence_hash'],approval_hash=digest(approval))
        return _public(loop,envelope,seal,state,records)
