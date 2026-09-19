"""Choose ONE pre-admitted whole stage from a closed loop menu.

Jev never authors a stage, never plans a tool call and never accepts a result.
It selects one item from a controller whitelist built from the parent's sealed
loop envelope, or returns the loop to the parent. The outbound boundary is the
existing `stage_transition` purpose; no new purpose is introduced.
"""
from __future__ import annotations
import re
from schema_validation import ContractError,digest
from typed_jev import MODEL,JevError,validate_reply
FIELDS={'goal_hash','iteration','max_iterations','last_status','last_accepted',
        'scope_violations','remaining_stage_ids','hard_owner_boundary'}
CONTROLS=('return_to_parent','stop')
MAX_ITERATIONS=4


def _shape(ctx):
    if type(ctx) is not dict or set(ctx)!=FIELDS:raise ContractError('loop_context_fields')
    for k in ('last_accepted','hard_owner_boundary'):
        if type(ctx[k]) is not bool:raise ContractError('loop_context_boolean')
    if not isinstance(ctx['goal_hash'],str) or not re.fullmatch('[0-9a-f]{64}',ctx['goal_hash']):
        raise ContractError('loop_context_goal_hash')
    for k in ('iteration','max_iterations'):
        if type(ctx[k]) is not int:raise ContractError('loop_context_shape')
    if not 0<=ctx['iteration']<=MAX_ITERATIONS or not 1<=ctx['max_iterations']<=MAX_ITERATIONS:
        raise ContractError('loop_context_shape')
    if not isinstance(ctx['last_status'],str) or not 1<=len(ctx['last_status'])<=128:
        raise ContractError('loop_context_shape')
    for k in ('scope_violations','remaining_stage_ids'):
        value=ctx[k]
        if (type(value) is not list or len(value)>16 or
            any(not isinstance(x,str) or not 1<=len(x)<=128 for x in value)):
            raise ContractError('loop_context_shape')
    stages=ctx['remaining_stage_ids']
    if len(set(stages))!=len(stages):raise ContractError('loop_context_shape')
    # The two controls are reserved words; a stage may never shadow them.
    if any(x in CONTROLS for x in stages):raise ContractError('reserved_stage_identity')


def allowed(ctx):
    _shape(ctx)
    if ctx['hard_owner_boundary'] or ctx['scope_violations']:return list(CONTROLS)
    if ctx['iteration']>0 and not ctx['last_accepted']:return list(CONTROLS)
    if ctx['iteration']>=ctx['max_iterations'] or not ctx['remaining_stage_ids']:return list(CONTROLS)
    return list(ctx['remaining_stage_ids'])+list(CONTROLS)


def _questions(choices):
    return {'next_stage':{'type':'choice',
            'instructions':'Choose only one whole pre-admitted stage from the controller whitelist, or return the loop to the parent. Do not plan, author or execute micro-actions.',
            'criteria':{k:k.replace('_',' ') for k in choices}}}


def choose(ctx,*,proposed=None,jev=None,admission=None,classification='synthetic',documents=(),source_guard=None):
    choices=allowed(ctx)
    r={'schema_version':1,'decision':CONTROLS[0],'context_hash':digest(ctx),'allowed':choices,
       'answers':{},'reason':'parent_default','source':'policy','confidence':{'choice':0.0,'margin':0.0},
       'usage':{'input_tokens':0,'output_tokens':0},'provider_model':None,'parent_acceptance':False}
    if proposed is not None:
        if proposed not in choices:raise ContractError('stage_not_allowed')
        r.update(decision=proposed,reason='parent_proposal',source='parent');return r
    if jev is None:return r
    questions=_questions(choices)
    try:
        from outbound_admission import require_admission
        policy=require_admission(admission)
        prepared=policy.prepare([({'loop_context':ctx},questions)],purpose='stage_transition',
            classification=classification,documents=documents,source_guard=source_guard)[0]
        reply=policy.evaluate(prepared,jev,purpose='stage_transition',classification=classification,
            source_guard=source_guard)
        if source_guard is not None:source_guard()
        a=validate_reply(reply,questions)['next_stage']
        r['answers']=reply['answers'];r['usage']=dict(reply['usage']);r['provider_model']=MODEL
        probabilities=sorted(a['probabilities'].values(),reverse=True)
        margin=probabilities[0]-(probabilities[1] if len(probabilities)>1 else 0.0)
        r['confidence']={'choice':a['confidence'],'margin':max(0.0,min(1.0,margin))}
        # Same starting thresholds as stage_transition; not measured accuracy.
        if a['confidence']>=.9 and probabilities[0]>=.9 and margin>=.2:
            r.update(decision=a['choice'],reason='typed_recommendation',source='jev')
    except (JevError,ValueError,TypeError,TimeoutError):pass
    return r
