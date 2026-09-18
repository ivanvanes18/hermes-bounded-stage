"""Choose one bounded next stage. No tool-call routing, permissions or execution."""
from __future__ import annotations
import re
from schema_validation import ContractError,digest
from typed_jev import JevError,validate_reply
from semantic_cascade import TRIAGE_CATEGORIES
FIELDS={'checks_passed','correction_used','parent_authorized_correction','evidence_hash','previous_evidence_hash',
        'scope_violations','triage_category','remaining_worker_calls','hard_owner_boundary'}


def allowed(ctx):
    if type(ctx) is not dict or set(ctx)!=FIELDS:raise ContractError('transition_context_fields')
    for k in ('checks_passed','correction_used','parent_authorized_correction','hard_owner_boundary'):
        if type(ctx[k]) is not bool:raise ContractError('transition_boolean')
    for k in ('evidence_hash','previous_evidence_hash'):
        v=ctx[k]
        if v is None and k=='previous_evidence_hash':continue
        if not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v):raise ContractError('transition_evidence_hash')
    if (type(ctx['scope_violations']) is not list or any(not isinstance(x,str) for x in ctx['scope_violations']) or
        ctx['triage_category'] not in TRIAGE_CATEGORIES or type(ctx['remaining_worker_calls']) is not int or
        not 0<=ctx['remaining_worker_calls']<=2):raise ContractError('transition_context_shape')
    choices=['return_to_owner','clarify','stop']
    if ctx['hard_owner_boundary']:return choices
    if ctx['checks_passed']:return ['complete']+choices
    if (not ctx['correction_used'] and ctx['parent_authorized_correction'] and
        ctx['evidence_hash']!=ctx['previous_evidence_hash'] and not ctx['scope_violations'] and
        ctx['triage_category']=='implementation_defect' and ctx['remaining_worker_calls']>0):
        choices.insert(0,'one_bounded_correction')
    return choices


def choose(ctx,*,proposed=None,jev=None,admission=None,classification='synthetic',documents=(),source_guard=None):
    choices=allowed(ctx)
    r={'schema_version':1,'transition':'return_to_owner','context_hash':digest(ctx),'evidence_hash':ctx['evidence_hash'],
       'allowed':choices,'answers':{},'reason':'owner_default','parent_acceptance':False}
    if proposed is not None:
        if proposed not in choices:raise ContractError('transition_not_allowed')
        r.update(transition=proposed,reason='parent_proposal');return r
    if jev is None:return r
    q={'next_stage':{'type':'choice','instructions':'Choose only the next whole stage from the controller whitelist. Do not plan or execute micro-actions.',
                     'criteria':{k:k.replace('_',' ') for k in choices}}}
    try:
        from outbound_admission import require_admission
        policy=require_admission(admission)
        prepared=policy.prepare([({'controller_context':ctx},q)],purpose='stage_transition',classification=classification,
            documents=documents,source_guard=source_guard)[0]
        reply=policy.evaluate(prepared,jev,purpose='stage_transition',classification=classification,source_guard=source_guard)
        if source_guard is not None:source_guard()
        a=validate_reply(reply,q)['next_stage'];r['answers']=reply['answers']
        probs=sorted(a['probabilities'].values(),reverse=True)
        if a['confidence']>=.9 and probs[0]>=.9 and (len(probs)<2 or probs[0]-probs[1]>=.2):
            r.update(transition=a['choice'],reason='typed_recommendation')
    except (JevError,ValueError,TypeError,TimeoutError):pass
    return r
