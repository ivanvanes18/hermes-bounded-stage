"""Serial staged executor controller. No daemon, core fork or global model routing.

Control state/checks/registry must be outside and inaccessible to the worker.
This code is not an operating-system sandbox. In this source candidate live
adapters and empirical feature gates remain separately blocked/unverified.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import secrets
import time
import executor_routes as routes
import harness_adapter as harness
from schema_validation import ContractError,canonical,digest,loads
from stage_contracts import (PROTECTED_NAMES,contract,file_hash,input_path,no_links,permission_hash,
                            read_file,validate_pins,validate_stage,worker_packet,workspace,_binding)
from typed_jev import TypedJev,JevError,validate_reply
from runtime_support import save_json as _save_json,StageError
TERMINAL={'needs_review','ready_for_parent_review','parent_accepted','stopped'}


def save(path,value):
    try:_save_json(Path(path),value)
    except (OSError,StageError):raise ContractError('state_write_failed') from None

def read(path):return loads(read_file(path,1024*1024))

@contextmanager
def lock(path):
    fd=None
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ContractError('one_writer_lock_busy') from None
        yield
    except OSError:raise ContractError('lock_unavailable') from None
    finally:
        if fd is not None:os.close(fd)


def snapshot(root):
    root=workspace(root);result={};total=0
    for directory,dirs,files in os.walk(root,followlinks=False):
        # Git administration is not a worker input. Record a worktree .git pointer
        # (if a file), but do not traverse a repository's internal object store.
        for name in list(dirs):
            p=Path(directory)/name
            if p.is_symlink():raise ContractError('workspace_symlink')
            if name=='.git':dirs.remove(name)
            elif name in ('secrets','auth','__pycache__'):raise ContractError('workspace_forbidden_directory')
        for name in sorted(files):
            p=Path(directory)/name
            if name=='.env' or name.startswith('.env.') or name.endswith('.pyc'):raise ContractError('workspace_forbidden_file')
            if p.is_symlink():raise ContractError('workspace_symlink')
            raw=read_file(p);total+=len(raw)
            if total>32*1024*1024 or len(result)>=4096:raise ContractError('workspace_budget')
            result[str(p.relative_to(root))]=hashlib.sha256(raw).hexdigest()
    return dict(sorted(result.items()))


def _scope(stage,before,after):
    changed=sorted(k for k in set(before)|set(after) if before.get(k)!=after.get(k))
    bad=set(changed)-set(stage['allowed_paths'])
    bad.update(k for k in before if k not in after)
    if len(changed)>stage['output_contract']['max_changed_files']:bad.add('__change_budget__')
    bad.update(k for k in stage['output_contract']['required_paths'] if k not in after)
    return changed,sorted(bad)


def initialize(stage,registry,run_dir,*,evidence_mode,gate_evidence=None):
    # Canonical deep copies prevent aliasing of a trusted parent's admitted data.
    stage=loads(canonical(stage));registry=loads(canonical(registry))
    validate_stage(stage);routes.validate_registry(registry)
    if evidence_mode not in ('synthetic','live'):raise ContractError('evidence_mode')
    if evidence_mode=='live':
        if stage['data_policy']['classification']=='synthetic':raise ContractError('synthetic_not_live')
        if any(r['adapter'] and r['adapter']['evidence_kind']=='synthetic' for r in registry['routes'] if r['route_id'] in stage['executor_candidates']):
            raise ContractError('synthetic_adapter_not_live')
    if any(stage['features'].values()) and evidence_mode=='live':
        _admit_gates(stage,gate_evidence)
    root=workspace(stage['workspace']);run=Path(run_dir).absolute()
    no_links(run.parent)
    if run.exists() or run.is_symlink():raise ContractError('run_already_exists')
    if run.is_relative_to(root) or root.is_relative_to(run):raise ContractError('controller_worker_overlap')
    if '.hermes' in run.parts and run.parts[run.parts.index('.hermes')+1:run.parts.index('.hermes')+2] != ('workspaces',):
        raise ContractError('active_profile_refused')
    initial=snapshot(root)
    with lock(root/'.staged-routing-workspace.json'):
        run.mkdir(mode=0o700);(run/'inputs').mkdir(mode=0o700);(run/'attempts').mkdir(mode=0o700)
        save(run/'stage.json',stage);save(run/'registry.json',registry);save(run/'initial-tree.json',initial)
        sealed_inputs={}
        for item in stage['inputs']:
            raw=read_file(input_path(root,item['path']))
            if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ContractError('input_changed_during_init')
            # IDs are labels, not filenames. Sequential names prevent path tricks.
            name=str(len(sealed_inputs))+'.bin';p=run/'inputs'/name
            with open(p,'xb') as f:f.write(raw)
            os.chmod(p,0o600)
            sealed_inputs[item['id']]={'path':'inputs/'+name,'sha256':item['sha256']}
        seal={'schema_version':1,'stage_hash':digest(stage),'registry_hash':digest(registry),'initial_tree_hash':digest(initial),
              'inputs':sealed_inputs,'evidence_mode':evidence_mode,'gate_evidence':gate_evidence}
        save(run/'seal.json',seal)
        state={'schema_version':1,'seal_hash':digest(seal),'status':'ready','reason':'initialized','attempts':0,'correction_used':False,
               'jev_calls':0,'jev_usage':{'input_tokens':0,'output_tokens':0},'worker_usage':{'input_tokens':0,'output_tokens':0},
               'created_at':time.time(),'deadline':time.time()+stage['limits']['max_seconds'],
               'result_hash':None,'result_path':None,'parent_accepted':False,'parent_approval':None,'last_transition':None}
        save(run/'state.json',state)
    return inspect(run)


def _admit_gates(stage,evidence):
    # An owner-reviewed record binds empirical evidence; it is not inferred from
    # test counts or fabricated live results. Stage 7 explicitly requires 4–6.
    required={'gate_4','gate_5','gate_6'} if stage['features']['stage_transition'] else {'gate_4'}
    if not isinstance(evidence,dict) or set(evidence)!={'approved_by','gates'} or evidence['approved_by']!='parent' or not isinstance(evidence['gates'],dict):
        raise ContractError('empirical_gate_pending')
    import re
    if not required<=set(evidence['gates']) or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in evidence['gates'].values()):
        raise ContractError('empirical_gate_pending')


def _load(run_dir):
    run=no_links(Path(run_dir).absolute())
    stage=read(run/'stage.json');reg=read(run/'registry.json');seal=read(run/'seal.json');state=read(run/'state.json')
    if (state.get('seal_hash')!=digest(seal) or seal.get('stage_hash')!=digest(stage) or seal.get('registry_hash')!=digest(reg)):
        raise ContractError('sealed_source_changed')
    validate_stage(stage,verify_files=False);routes.validate_registry(reg)
    if type(state.get('attempts')) is not int or not 0<=state['attempts']<=stage['limits']['max_worker_calls']:
        raise ContractError('state_attempt_budget')
    if type(state.get('jev_calls')) is not int or not 0<=state['jev_calls']<=stage['limits']['max_jev_calls']:
        raise ContractError('state_jev_budget')
    if state.get('status') not in TERMINAL|{'ready','worker_running','correction_ready'}:raise ContractError('state_status')
    if type(state.get('correction_used')) is not bool or type(state.get('parent_accepted')) is not bool:raise ContractError('state_type')
    if (state['status']=='parent_accepted')!=state['parent_accepted']:raise ContractError('state_acceptance')
    if type(state.get('deadline')) not in (int,float) or not __import__('math').isfinite(state['deadline']):raise ContractError('state_deadline')
    initial=read(run/'initial-tree.json')
    if digest(initial)!=seal['initial_tree_hash']:raise ContractError('initial_tree_changed')
    for item in seal['inputs'].values():
        if file_hash(run/item['path'])!=item['sha256']:raise ContractError('sealed_input_changed')
    for binding in stage['methodology']['domain_skills']+[stage['methodology']['process_owner']]:_binding(binding)
    for check in stage['checks']:validate_pins(check,stage['workspace'])
    if stage.get('deterministic_action') is not None:validate_pins(stage['deterministic_action'],stage['workspace'])
    for c in stage['context']['candidates']:
        if file_hash(c['source_path'])!=c['source_sha256']:raise ContractError('context_source_changed')
    return run,stage,reg,seal,state,initial


def _last(run,state):
    if state['result_path'] is None:return None
    # Only this deterministic path is valid; state cannot redirect reads.
    expected=f'attempts/{state["attempts"]}/result.json'
    if state['result_path']!=expected:raise ContractError('result_path_binding')
    value=read(run/expected)
    if digest(value)!=state['result_hash']:raise ContractError('result_changed')
    return value


def _public(run,stage,seal,state):
    return {'status':state['status'],'reason':state['reason'],'stage_id':stage['stage_id'],'run_directory':str(run),
        'evidence_kind':seal['evidence_mode'],'attempts':state['attempts'],'correction_used':state['correction_used'],
        'jev_calls':state['jev_calls'],'jev_usage':state['jev_usage'],'worker_usage':state['worker_usage'],
        'result_hash':state['result_hash'],'parent_accepted':state['parent_accepted'],'last_result':_last(run,state),
        'live_readiness_claim':False,'last_transition':state['last_transition']}


def inspect(run_dir):
    run,stage,reg,seal,state,initial=_load(run_dir)
    result=_last(run,state)
    if result and state['status'] in ('ready_for_parent_review','parent_accepted'):
        if digest(snapshot(stage['workspace']))!=result['final_tree_hash']:raise ContractError('result_workspace_changed')
    return _public(run,stage,seal,state)


def _remaining(state):
    left=state['deadline']-time.time()
    if left<=0:raise ContractError('deadline_exceeded')
    return left


class _Provider:
    """Persistent call reservation before every provider attempt, no auto-retry."""
    def __init__(self,run,stage,seal,state,provider,purpose):
        self.run=run;self.stage=stage;self.seal=seal;self.state=state;self.provider=provider;self.purpose=purpose
    def evaluate(self,data,questions):
        if self.provider is None:raise JevError('provider_unavailable')
        if self.seal['evidence_mode']=='live':
            if not self.stage['data_policy']['external_allowed'] or not isinstance(self.provider,TypedJev) or self.provider.purpose!=self.purpose:
                raise JevError('outbound_not_authorized')
        _remaining(self.state)
        if self.state['jev_calls']>=self.stage['limits']['max_jev_calls']:raise JevError('jev_budget_exhausted')
        self.state['jev_calls']+=1;save(self.run/'state.json',self.state)
        try:
            reply=self.provider.evaluate(data,questions);validate_reply(reply,questions)
            _remaining(self.state)
            for k in self.state['jev_usage']:self.state['jev_usage'][k]+=reply['usage'][k]
            save(self.run/'state.json',self.state);return reply
        except (JevError,ContractError,TimeoutError,ValueError,TypeError,KeyError):raise JevError('provider_unavailable') from None


def _routing_admission_evidence(stage,run,seal):
    """Return full local evidence for classification plus an exact send-time guard."""
    bindings=[];documents=[{'stage':stage}]
    def bound_text(path,expected,kind,identity):
        raw=read_file(path)
        if hashlib.sha256(raw).hexdigest()!=expected:raise ContractError('routing_source_changed')
        bindings.append((path,expected))
        try:text=raw.decode('utf-8')
        except UnicodeError:raise ContractError('routing_source_encoding') from None
        documents.append({'kind':kind,'id':identity,'text':text})
    for identity,item in seal['inputs'].items():
        bound_text(run/item['path'],item['sha256'],'input',identity)
    for item in stage['methodology']['domain_skills']+[stage['methodology']['process_owner']]:
        bound_text(Path(item['source_path']),item['sha256'],'methodology',item['name'])
    for item in stage['context']['candidates']:
        bound_text(Path(item['source_path']),item['source_sha256'],'context',item['id'])
    def guard():
        for path,expected in bindings:
            if file_hash(path)!=expected:raise ContractError('routing_source_changed')
    return documents,guard


def advance(run_dir,*,jev=None,fixed_route=None,providers=None,admissions=None):
    run,stage,reg,seal,state,initial=_load(run_dir)
    root=workspace(stage['workspace'])
    with lock(run/'seal.json'),lock(root/'.staged-routing-workspace.json'):
        # Reload under both locks; another controller may have advanced the run.
        run,stage,reg,seal,state,initial=_load(run)
        if state['status'] in TERMINAL:return inspect(run)
        if state['status']=='worker_running':
            state.update(status='needs_review',reason='interrupted_worker_no_implicit_retry');save(run/'state.json',state)
            return _public(run,stage,seal,state)
        _remaining(state)
        before=snapshot(root)
        if state['attempts']==0 and before!=initial:raise ContractError('input_drift_before_execution')
        if state['attempts']>=stage['limits']['max_worker_calls']:raise ContractError('worker_budget')
        if state['attempts']:
            if not state['correction_used'] or state['status']!='correction_ready':raise ContractError('unapproved_retry')
            previous=_last(run,state)
            if not previous or digest(before)!=previous['final_tree_hash']:raise ContractError('correction_baseline_changed')
        provider_map=providers or {};admission_map=admissions or {}
        def provider(purpose):return _Provider(run,stage,seal,state,provider_map.get(purpose,jev),purpose)
        readiness=harness.readiness(stage,reg,evidence_mode=seal['evidence_mode']) if not routes.hard_owner(stage) else {}
        eligible=routes.available(stage,reg,readiness)
        reply=None
        if fixed_route is None and eligible!=['owner'] and not routes.hard_owner(stage):
            q=routes.request(stage,reg,readiness)
            try:
                from outbound_admission import require_admission
                documents,routing_guard=_routing_admission_evidence(stage,run,seal)
                policy=require_admission(admission_map.get('executor_routing'))
                prepared=policy.prepare([(q['state'],q['questions'])],purpose='executor_routing',
                    classification=stage['data_policy']['classification'],documents=documents,source_guard=routing_guard)[0]
                reply=policy.evaluate(prepared,provider('executor_routing'),purpose='executor_routing',
                    classification=stage['data_policy']['classification'],source_guard=routing_guard)
                routing_guard()
            except (JevError,ContractError,UnicodeError):pass
        receipt=routes.decide(stage,reg,readiness,reply,fixed_route=fixed_route)
        route=next(r for r in reg['routes'] if r['route_id']==receipt['selected_route'])
        if route['kind']=='control':
            state.update(status='stopped' if route['route_id']=='stop' else 'needs_review',reason=receipt['reason'])
            save(run/'control-route-receipt.json',receipt);save(run/'state.json',state)
            return _public(run,stage,seal,state)
        selected_context=[];context_result=None
        if stage['context']['candidates'] or stage['context']['mandatory_ids']:
            from context_rerank import select_context,materialize_selected
            context_result=select_context(stage['objective'],stage['context'],
                provider('context_reranking') if stage['features']['context_rerank'] else None,
                max_chars=stage['limits']['max_context_chars'],
                admission=admission_map.get('context_reranking'),classification=stage['data_policy']['classification'])
            if context_result['status']!='selected':
                state.update(status='needs_review',reason='context_unavailable');save(run/'context-receipt.json',context_result);save(run/'state.json',state)
                return _public(run,stage,seal,state)
            try:selected_context=materialize_selected(stage['context'],context_result)
            except (ContractError,UnicodeError):
                state.update(status='needs_review',reason='context_source_changed');save(run/'state.json',state)
                return _public(run,stage,seal,state)
        attempt=state['attempts']+1;folder=run/'attempts'/str(attempt);folder.mkdir(mode=0o700)
        previous=_last(run,state)
        execution={'attempt':attempt,'baseline_tree_hash':digest(before),
            'current_input_hashes':{i['id']:before[i['path']] for i in stage['inputs']},
            'previous_evidence_hash':state['result_hash'],
            'correction_evidence':[{'check_id':c['id'],'passed':c['passed'],'reason':c['reason']}
                for c in previous['checks']] if previous else []}
        nonce=secrets.token_hex(16);packet=worker_packet(stage,receipt,nonce,selected_context,execution=execution)
        save(folder/'route-receipt.json',receipt);save(folder/'worker-packet.json',packet)
        if context_result:save(folder/'context-receipt.json',context_result)
        state.update(status='worker_running',reason='worker_started',attempts=attempt)
        save(run/'state.json',state)  # consume before side effects; crash != permission to retry
        proof=None;worker_usage={'input_tokens':0,'output_tokens':0};worker_error=None;elapsed_ms=0
        try:
            if route['kind']=='deterministic':
                action=stage.get('deterministic_action')
                if action is None:raise ContractError('deterministic_action_missing')
                got=harness.run_command(action,cwd=root,worker_root=root,timeout_seconds=_remaining(state))
                elapsed_ms=got['elapsed_ms']
                if got['exit_code']!=0:raise ContractError('deterministic_action_failed')
            else:
                got=harness.execute(route,packet,root,evidence_mode=seal['evidence_mode'],timeout_seconds=_remaining(state))
                proof=got['proof'];worker_usage=got['usage'];elapsed_ms=got['elapsed_ms']
        except (ContractError,OSError) as exc:
            worker_error=str(exc) if isinstance(exc,ContractError) else 'worker_io_error'
        checks=[];after={};scope=[];changed=[]
        try:
            after=snapshot(root);changed,scope=_scope(stage,initial,after)
            # Checks run independently of worker exit/self-report. No Jev on a
            # deterministic failure, including permission/scope/proof failure.
            if not worker_error:
                for check in stage['checks']:
                    try:
                        got=harness.run_command(check,cwd=root,worker_root=root,timeout_seconds=_remaining(state))
                        checks.append({'id':check['id'],'passed':got['exit_code']==check['expected_exit'],
                            'exit_code':got['exit_code'],'elapsed_ms':got['elapsed_ms'],
                            'stdout_sha256':hashlib.sha256(got['stdout']).hexdigest(),'reason':'checked'})
                    except ContractError as e:
                        checks.append({'id':check['id'],'passed':False,'exit_code':None,'elapsed_ms':0,'stdout_sha256':None,'reason':str(e)})
            after_checks=snapshot(root)
            if after_checks!=after:
                scope.append('__checks_mutated_workspace__');after=after_checks
        except ContractError as e:worker_error=worker_error or str(e);scope.append('__workspace_invalid__')
        passed=(not worker_error and not scope and len(checks)==len(stage['checks']) and all(c['passed'] for c in checks))
        semantic=None
        if stage['features']['semantic_cascade']:
            from semantic_cascade import cascade
            try:
                # A failed deterministic gate does not need to parse evidence
                # (which may be binary), and never invokes the semantic model.
                source_text={};outputs={};bindings=[]
                if passed:
                    for key,item in seal['inputs'].items():
                        path=run/item['path'];raw=read_file(path)
                        if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ContractError('sealed_input_changed')
                        source_text[key]=raw.decode('utf-8');bindings.append((path,item['sha256']))
                    for path in stage['output_contract']['required_paths']:
                        raw=read_file(root/path)
                        if hashlib.sha256(raw).hexdigest()!=after[path]:raise ContractError('checked_output_changed')
                        outputs[path]=raw.decode('utf-8');bindings.append((root/path,after[path]))
                def semantic_source_guard():
                    for path,expected in bindings:
                        if file_hash(path)!=expected:raise ContractError('semantic_source_changed')
                semantic=cascade(passed,stage['semantic_flags'],source_text,outputs,provider('semantic_cascade'),
                    admission=admission_map.get('semantic_cascade'),classification=stage['data_policy']['classification'],
                    source_guard=semantic_source_guard)
            except (UnicodeError,ContractError):
                semantic={'status':'needs_review','reason':'semantic_evidence_encoding_or_source','scores':{},'exceptions':[],'parent_acceptance':False}
            passed=passed and semantic['status']=='clear'
            save(folder/'semantic-receipt.json',semantic)
        # Detect mutations during semantic calls and before publishing evidence.
        try:
            if after!=snapshot(root):scope.append('__post_check_source_drift__');passed=False
            for c in stage['context']['candidates']:
                if file_hash(c['source_path'])!=c['source_sha256']:raise ContractError('context_source_changed')
            for binding in stage['methodology']['domain_skills']+[stage['methodology']['process_owner']]:_binding(binding)
            for check in stage['checks']:validate_pins(check,root)
        except ContractError:scope.append('__post_check_source_drift__');passed=False
        result={'schema_version':1,'attempt':attempt,'stage_hash':digest(stage),'packet_hash':digest(packet),
            'route_receipt_hash':digest(receipt),'selected_route':route['route_id'],'executor_skill':route['skill'],
            'proof':proof,'evidence_kind':seal['evidence_mode'],'worker_error':worker_error,'checks':checks,
            'scope_violations':sorted(set(scope)),'changed_paths':changed,'initial_tree_hash':digest(initial),'final_tree_hash':digest(after),
            'artifacts':{p:after[p] for p in stage['output_contract']['required_paths'] if p in after},
            'elapsed_ms':elapsed_ms,'worker_usage':worker_usage,'semantic':semantic,'checks_and_policy_passed':bool(passed),
            'parent_acceptance':False}
        save(folder/'result.json',result)
        for k in state['worker_usage']:state['worker_usage'][k]+=worker_usage[k]
        state.update(result_hash=digest(result),result_path=f'attempts/{attempt}/result.json',
                     status='ready_for_parent_review' if passed else 'needs_review',
                     reason='independent_checks_passed' if passed else worker_error or ('scope_violation' if scope else 'deterministic_or_semantic_check_failed'))
        save(run/'state.json',state)
        return _public(run,stage,seal,state)


def accept(run_dir,approval):
    run,stage,reg,seal,state,initial=_load(run_dir)
    with lock(run/'seal.json'),lock(workspace(stage['workspace'])/'.staged-routing-workspace.json'):
        run,stage,reg,seal,state,initial=_load(run)
        if (not isinstance(approval,dict) or set(approval)!={'decision','evidence_hash','reviewer','evidence_kind'} or
            approval['decision']!='accept' or approval['evidence_hash']!=state['result_hash'] or approval['evidence_kind']!=seal['evidence_mode'] or
            not isinstance(approval['reviewer'],str) or not 1<=len(approval['reviewer'])<=128 or state['status']!='ready_for_parent_review'):
            raise ContractError('parent_acceptance_binding')
        result=_last(run,state)
        if not result['checks_and_policy_passed'] or digest(snapshot(stage['workspace']))!=result['final_tree_hash']:
            raise ContractError('acceptance_evidence_changed')
        state.update(status='parent_accepted',parent_accepted=True,parent_approval=approval,reason='explicit_parent_acceptance')
        save(run/'state.json',state);return _public(run,stage,seal,state)


def transition(run_dir,*,proposed=None,jev=None,providers=None,admissions=None,parent_authorized_correction=False,triage_category=None):
    """Trusted parent call: schedule one whole correction, never execute it here."""
    from stage_transition import choose
    from semantic_cascade import triage,TRIAGE_CATEGORIES
    run,stage,reg,seal,state,initial=_load(run_dir)
    with lock(run/'seal.json'),lock(workspace(stage['workspace'])/'.staged-routing-workspace.json'):
        run,stage,reg,seal,state,initial=_load(run)
        if not stage['features']['stage_transition']:raise ContractError('stage_transition_disabled')
        if state['status'] not in ('needs_review','ready_for_parent_review'):raise ContractError('transition_state_not_allowed')
        _remaining(state);last=_last(run,state)
        if last is None:raise ContractError('transition_missing_evidence')
        if state['last_transition'] and state['last_transition']['evidence_hash']==state['result_hash']:
            raise ContractError('transition_repeated_evidence')
        if digest(snapshot(stage['workspace']))!=last['final_tree_hash']:raise ContractError('transition_evidence_changed')
        admission_map=admissions or {}
        def transition_guard():
            current=_last(run,state)
            if current is None or digest(current)!=state['result_hash'] or digest(snapshot(stage['workspace']))!=last['final_tree_hash']:
                raise ContractError('transition_evidence_changed')
        facts={'deterministic_passed':last['checks_and_policy_passed'],'expected_red':False,
               'environment_failure':last['worker_error'] in ('adapter_unavailable','adapter_timeout'),
               'missing_dependency':False,'missing_evidence':not bool(last['checks']),
               'scope_mismatch':bool(last['scope_violations']),'stale_plan':False}
        if triage_category is not None:
            if triage_category not in TRIAGE_CATEGORIES:raise ContractError('triage_category_invalid')
            diagnosis={'category':triage_category,'reason':'parent_classification','automatic_fix':False}
        else:
            diagnosis=triage(facts,_Provider(run,stage,seal,state,(providers or {}).get('triage',jev),'triage'),
                admission=admission_map.get('triage'),classification=stage['data_policy']['classification'],
                documents=[{'facts':facts,'result':last}],source_guard=transition_guard)
        ctx={'checks_passed':last['checks_and_policy_passed'],'correction_used':state['correction_used'],
             'parent_authorized_correction':parent_authorized_correction,'evidence_hash':state['result_hash'],
             'previous_evidence_hash':state['last_transition']['evidence_hash'] if state['last_transition'] else None,
             'scope_violations':last['scope_violations'],'triage_category':diagnosis['category'],
             'remaining_worker_calls':stage['limits']['max_worker_calls']-state['attempts'],
             'hard_owner_boundary':routes.hard_owner(stage)}
        decision=choose(ctx,proposed=proposed,jev=_Provider(run,stage,seal,state,(providers or {}).get('stage_transition',jev),'stage_transition'),
            admission=admission_map.get('stage_transition'),classification=stage['data_policy']['classification'],
            documents=[{'controller_context':ctx,'diagnosis':diagnosis,'result':last}],source_guard=transition_guard)
        decision['triage']=diagnosis
        if decision['transition']=='one_bounded_correction':
            state.update(status='correction_ready',correction_used=True,reason='parent_authorized_single_correction')
        elif decision['transition']=='complete':
            state.update(status='ready_for_parent_review',reason='complete_pending_parent_acceptance')
        else:
            state.update(status='stopped' if decision['transition']=='stop' else 'needs_review',reason=decision['transition'])
        state['last_transition']=decision;save(run/'state.json',state)
        return _public(run,stage,seal,state)
