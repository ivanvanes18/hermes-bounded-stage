"""Trusted role registry and a single stage-level typed decision; no model name execution."""
from __future__ import annotations
import time
import math
from pathlib import Path
from stage_contracts import (ContractError,PROOF_FIELDS,contract,digest,file_hash,loads,permission_hash,
                             validate_pins,validate_stage)
from typed_jev import MODEL,JevError,validate_reply
LEVEL={'low':0,'medium':1,'high':2}
CONTROLS={'owner','clarify','stop'}


def validate_registry(registry,*,require_adapters=True):
    contract(registry,'executor-registry.schema.json')
    by_id={r['route_id']:r for r in registry['routes']}
    if len(by_id)!=len(registry['routes']) or not CONTROLS<=set(by_id):raise ContractError('registry_identity')
    for r in registry['routes']:
        if r['kind']=='control':
            if r['route_id'] not in CONTROLS or r['mode']!='control' or r['status']!='control-approved' or any(r[k] is not None for k in ('adapter','harness','model','effort','skill','fallback')):
                raise ContractError('control_route_shape')
        elif r['kind']=='executor':
            if not r['skill'] or not r['harness'] or r['mode']=='control' or set(r['required_runtime_proof'])!=set(PROOF_FIELDS):
                raise ContractError('executor_route_shape')
            if r['status'] not in ('disabled','unverified','approved-for-pilot'):raise ContractError('executor_status')
            if r['status']=='approved-for-pilot':
                if not all(r[k] for k in ('model','effort','approval_sha256')):raise ContractError('route_approval_missing')
                if r['model'].casefold() in ('auto','default','sonnet','opus','luna','latest') or 'latest' in r['model'].casefold():
                    raise ContractError('model_alias_not_exact')
                if r['mode']=='bounded_write' and not r['write_benchmark_sha256']:raise ContractError('write_benchmark_missing')
                if require_adapters and r['adapter'] is None:raise ContractError('trusted_adapter_missing')
        else:
            if r['route_id']!='deterministic' or r['status']!='deterministic-approved' or any(r[k] is not None for k in ('harness','model','effort','adapter')):
                raise ContractError('deterministic_route_shape')
        if r['fallback'] is not None and (r['fallback'] not in by_id or r['fallback']==r['route_id']):raise ContractError('fallback_unknown_or_cycle')
        if r['adapter'] is not None and require_adapters:validate_pins(r['adapter'])
    for start in by_id:
        seen=set();current=start
        while current is not None:
            if current in seen:raise ContractError('fallback_cycle')
            seen.add(current);current=by_id[current]['fallback']


def load_registry(path,expected_sha256):
    if file_hash(path)!=expected_sha256:raise ContractError('registry_hash_mismatch')
    from stage_contracts import read_file
    reg=loads(read_file(path));validate_registry(reg)
    if file_hash(path)!=expected_sha256:raise ContractError('registry_changed')
    return reg


def hard_owner(stage):return stage['risk']['level']=='high' or any(v for k,v in stage['risk'].items() if k!='level')


def _ready_ok(stage,reg,r,record):
    try:
        if not record.get('ready') or record['route_hash']!=digest(r) or record['registry_hash']!=digest(reg) or record['stage_hash']!=digest(stage):return False
        if type(record['expires_at']) not in (int,float) or not math.isfinite(record['expires_at']) or record['expires_at']<=time.time():return False
        if any(record[k]!=r[k] for k in ('model','harness','effort')) or stage['mode'] not in record['supported_modes']:return False
        if r['adapter'] is not None and record['adapter_digest']!=digest(r['adapter']):return False
        return True
    except (KeyError,TypeError):return False


def available(stage,registry,readiness):
    validate_stage(stage,verify_files=False);validate_registry(registry,require_adapters=False)
    by_id={r['route_id']:r for r in registry['routes']}
    if any(x not in by_id for x in stage['executor_candidates']):raise ContractError('unknown_route')
    if hard_owner(stage):return ['owner']
    ready=[]
    for rid in stage['executor_candidates']:
        r=by_id[rid]
        if r['kind']=='control':ready.append(rid);continue
        if LEVEL[stage['risk']['level']]>LEVEL[r['max_risk']] or stage['mode']!=r['mode'] or stage['role'] not in r['roles']:continue
        if r['kind']=='deterministic':
            if stage.get('deterministic_action') is None:continue
            # Deterministic operations are selected explicitly by the parent and
            # executed from a separate pinned action, never from model text.
            ready.append(rid)
        elif r['status']=='approved-for-pilot' and _ready_ok(stage,registry,r,readiness.get(rid,{})):
            policy=stage['data_policy']
            if policy['classification']=='private':continue
            if policy['classification']!='synthetic' and not policy['external_allowed']:continue
            ready.append(rid)
    return sorted(set(ready)|{'owner'})


def request(stage,registry,readiness):
    eligible=available(stage,registry,readiness);by_id={r['route_id']:r for r in registry['routes']}
    state={'objective':stage['objective'],'mode':stage['mode'],'role':stage['role'],
        'risk_level':stage['risk']['level'],'check_count':len(stage['checks']),
        'domain_skills':[d['name'] for d in stage['methodology']['domain_skills']],
        'process_owner':stage['methodology']['process_owner']['name'],
        'routes':[{'route_id':r,'max_complexity':by_id[r]['max_complexity'],'kind':by_id[r]['kind']} for r in eligible]}
    questions={'route':{'type':'choice','instructions':'Choose the minimally sufficient allowed executor for this entire stage, never a tool call. Parent owns methods and final acceptance.',
               'criteria':{r:('Return the stage to the parent.' if r=='owner' else 'Use this admitted role: '+by_id[r]['kind']+', max complexity '+by_id[r]['max_complexity']) for r in eligible}},
        'complexity':{'type':'choice','instructions':'Classify complexity of this bounded stage, not an entire project.',
                      'criteria':{'low':'Mechanical or simple local change.','medium':'Several interacting constraints.','high':'Architectural or ambiguous reasoning.'}}}
    for k,instructions in [('risk','Does executing this stage create an unhandled material risk? True means risk/problem.'),
                            ('verifiability','Can the defined checks verify the requested result? True means verifiable.'),
                            ('context_sufficiency','Is the supplied task description sufficient to choose a route? True means sufficient.')]:
        questions[k]={'type':'noul','instructions':instructions,'criteria':{'true':'The proposition is true.','false':'The proposition is false.'}}
    return {'model':MODEL,'state':state,'questions':questions}


def decide(stage,registry,readiness,reply=None,*,fixed_route=None):
    eligible=available(stage,registry,readiness);by_id={r['route_id']:r for r in registry['routes']}
    selected='owner';reason='provider_unavailable';overrides=[];answers={};confidence={'route':0.0,'margin':0.0}
    source='policy';usage={'input_tokens':0,'output_tokens':0};provider_model=None
    if hard_owner(stage):reason='hard_owner_boundary';overrides.append(reason)
    elif fixed_route is not None:
        if fixed_route not in eligible:raise ContractError('fixed_route_not_eligible')
        selected=fixed_route;reason='parent_fixed_route';source='fixed'
    elif eligible==['owner']:reason='no_ready_executor'
    else:
        try:
            questions=request(stage,registry,readiness)['questions'];answers=validate_reply(reply,questions)
            source='jev';provider_model=MODEL;usage=reply['usage']
            a=answers['route'];probs=sorted(a['probabilities'].values(),reverse=True)
            margin=probs[0]-(probs[1] if len(probs)>1 else 0.0)
            confidence={'route':a['confidence'],'margin':max(0.0,min(1.0,margin))}
            choice=a['choice'];complexity=answers['complexity']
            if a['confidence']<.8 or a['probabilities'][choice]<.9 or margin<.2:
                reason='low_confidence'
            elif answers['risk']['noul']>.1:reason='semantic_risk'
            elif answers['verifiability']['noul']<.9 or answers['context_sufficiency']['noul']<.9:reason='insufficient_verifiability_or_context'
            elif complexity['confidence']<.8 or complexity['probabilities'][complexity['choice']]<.9:reason='complexity_uncertain'
            elif LEVEL[complexity['choice']]>LEVEL[by_id[choice]['max_complexity']]:reason='route_complexity_mismatch'
            else:selected=choice;reason='admitted'
        except (JevError,KeyError,TypeError):
            answers={};reason='provider_unavailable';source='policy';provider_model=None
    receipt={'schema_version':1,'policy_version':'executor-policy-1','stage_hash':digest(stage),'methodology_hash':digest(stage['methodology']),
        'registry_hash':digest(registry),'selected_route':selected,'selected_route_hash':digest(by_id[selected]),'eligible_routes':eligible,
        'readiness':readiness,'answers':answers,'confidence':confidence,'hard_overrides':overrides,'reason':reason,'source':source,
        'usage':usage,'provider_model':provider_model,'permissions_hash':permission_hash(stage),'checks_hash':digest(stage['checks']),
        'parent_acceptance':False}
    contract(receipt,'route-receipt.schema.json')
    return loads(__import__('schema_validation').canonical(receipt))
