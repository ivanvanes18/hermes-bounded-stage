"""New /bs routing envelope. The inherited v1/v2 plan validator is unchanged."""
from __future__ import annotations
from functools import lru_cache
import hashlib
from pathlib import Path,PurePosixPath
import re
from pinned_runtime import read_document,pinned_hash
from schema_validation import ContractError,canonical,digest,loads,validate
SCHEMAS=Path(__file__).resolve().parents[1]/'assets/schemas'
PROOF_FIELDS=['harness','model','effort','session_id','permissions','packet_hash','nonce','exit_code']
MAX_FILE=16*1024*1024
PROTECTED_NAMES={'.git','.env','auth','secrets','.staged-routing-workspace.json'}

@lru_cache(maxsize=16)
def schema(name):return loads((SCHEMAS/name).read_bytes())

def contract(value,name):validate(value,schema(name))

def clean_relative(value):
    if not isinstance(value,str) or '\\' in value or '\x00' in value or not value:
        raise ContractError('invalid_relative_path')
    p=PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or '.' in p.parts or str(p)!=value or p.parts[0] in PROTECTED_NAMES:
        raise ContractError('invalid_relative_path')
    return p

def no_links(path):
    p=Path(path)
    if not p.is_absolute() or p!=p.absolute() or str(p)!=str(p.resolve()):raise ContractError('noncanonical_path')
    for part in (p,*p.parents):
        if part.is_symlink():raise ContractError('symlink_refused')
    return p

def read_file(path,limit=MAX_FILE):
    return read_document(path,limit)

def file_hash(path):return hashlib.sha256(read_file(path)).hexdigest()

def workspace(path):
    root=no_links(path)
    if not root.is_dir():raise ContractError('workspace_missing')
    parts=root.parts
    if '.hermes' in parts:
        at=parts.index('.hermes')
        if len(parts)<=at+1 or parts[at+1]!='workspaces':raise ContractError('active_profile_refused')
    marker=loads(read_file(root/'.staged-routing-workspace.json',4096))
    if marker!={'purpose':'hermes-routing-staged','schema_version':1}:raise ContractError('staged_marker_missing')
    return root

def input_path(root,relative):
    clean_relative(relative);p=no_links(Path(root)/relative)
    if not p.is_relative_to(Path(root)):raise ContractError('path_escape')
    return p

def validate_pins(command,worker_root=None):
    args=command['argv'];pins=command['pins']
    if any('{' in a or '}' in a for a in args if a!='{workspace}'):
        raise ContractError('argument_placeholder')
    absolute={a for a in args if Path(a).is_absolute()}
    if not args or not Path(args[0]).is_absolute() or set(pins)!=absolute:
        raise ContractError('incomplete_command_pins')
    if Path(args[0]).name.lower() in ('sh','bash','zsh','fish','dash','cmd','powershell','pwsh') or '-c' in args:
        raise ContractError('shell_not_allowed')
    # Optional. argv[0] is native by definition; native_pins names ADDITIONAL native
    # executables only. `set(pins)==absolute` above already forces every entry to be an
    # absolute argv item, so no membership test is needed and none is reachable.
    native=command.get('native_pins') or []
    if type(native) is not list or len(native)>4 or len(set(native))!=len(native):raise ContractError('native_pin_shape')
    if not set(native)<=set(pins) or args[0] in native:raise ContractError('native_pin_not_pinned')
    native=set(native)
    for path,expected in pins.items():
        p=no_links(path)
        if worker_root and p.is_relative_to(Path(worker_root)):
            raise ContractError('controller_code_worker_writable')
        # The executable limit and the +x/no-setuid check must apply here too: this site
        # runs before bind_command and would otherwise reject a native ELF as source_size.
        if pinned_hash(p,executable=(path==args[0] or path in native))!=expected:raise ContractError('command_pin_changed')

def _binding(item):
    raw=read_file(item['source_path'],256*1024)
    if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ContractError('methodology_source_changed')
    try:text=raw.decode('utf-8')
    except UnicodeError:raise ContractError('methodology_encoding') from None
    if not text.startswith('---\n'):raise ContractError('methodology_frontmatter_missing')
    header=text.split('---',2)[1]
    for key in ('name','version'):
        match=re.search(r'^'+key+r':\s*["\']?([^\n"\']+)["\']?\s*$',header,re.M)
        if not match or match.group(1).strip()!=item[key]:raise ContractError('methodology_identity_mismatch')
    return text

def validate_stage(stage,*,verify_files=True):
    contract(stage,'stage-envelope.schema.json')
    for key in ('inputs','checks'):
        names=[x['id'] for x in stage[key]]
        if len(names)!=len(set(names)):raise ContractError('duplicate_identity')
    names=[x['name'] for x in stage['methodology']['domain_skills']]
    if len(names)!=len(set(names)) or stage['methodology']['process_owner']['name'] in names:
        raise ContractError('methodology_role_conflict')
    if stage['mode']=='read_only' and stage['allowed_paths']:raise ContractError('readonly_write_scope')
    for value in stage['allowed_paths']+stage['output_contract']['required_paths']+[x['path'] for x in stage['inputs']]:clean_relative(value)
    if not set(stage['output_contract']['required_paths'])<=(set(stage['allowed_paths'])|{x['path'] for x in stage['inputs']}):
        raise ContractError('output_outside_scope')
    if stage['output_contract']['max_changed_files']>len(stage['allowed_paths']):raise ContractError('change_budget_outside_scope')
    domains={x['name']:x for x in stage['methodology']['domain_skills']}
    rules=stage['methodology']['domain_rules']
    if len({r['rule_id'] for r in rules})!=len(rules):raise ContractError('duplicate_rule')
    for rule in rules:
        if rule['skill'] not in domains or rule['source_sha256']!=domains[rule['skill']]['sha256']:
            raise ContractError('rule_binding_mismatch')
    if stage['data_policy']['classification']=='private' and stage['data_policy']['external_allowed']:
        raise ContractError('private_external_send')
    candidates=stage['context']['candidates'];ids={c['id'] for c in candidates}
    if len(ids)!=len(candidates) or not set(stage['context']['mandatory_ids'])<=ids:raise ContractError('context_identity')
    for c in candidates:
        if c['start_line']>c['end_line']:raise ContractError('context_line_range')
    for f in stage['semantic_flags']:
        clean_relative(f['output_path'])
        if f['source_id'] not in {i['id'] for i in stage['inputs']} or f['output_path'] not in stage['output_contract']['required_paths']:
            raise ContractError('semantic_evidence_binding')
    if not verify_files:return
    root=workspace(stage['workspace'])
    for item in stage['inputs']:
        if file_hash(input_path(root,item['path']))!=item['sha256']:raise ContractError('input_source_changed')
    docs={name:_binding(item) for name,item in domains.items()};_binding(stage['methodology']['process_owner'])
    for rule in rules:
        if rule['text'] not in docs[rule['skill']] or rule['text']==docs[rule['skill']]:raise ContractError('rule_not_anchored_excerpt')
    for command in stage['checks']:validate_pins(command,root)
    if stage.get('deterministic_action') is not None:validate_pins(stage['deterministic_action'],root)
    for c in candidates:
        if file_hash(c['source_path'])!=c['source_sha256']:raise ContractError('context_source_changed')

def permission_hash(stage):return digest({'workspace':stage['workspace'],'mode':stage['mode'],'allowed_paths':stage['allowed_paths'],
                                         'risk':stage['risk'],'role':stage['role']})

def worker_packet(stage,receipt,nonce,selected_context=(),*,execution=None):
    validate_stage(stage,verify_files=False);contract(receipt,'route-receipt.schema.json')
    if (receipt['stage_hash']!=digest(stage) or receipt['methodology_hash']!=digest(stage['methodology']) or
        receipt['permissions_hash']!=permission_hash(stage) or receipt['checks_hash']!=digest(stage['checks'])):
        raise ContractError('route_binding_mismatch')
    bindings=stage['methodology']
    packet={'schema_version':1,'stage_id':stage['stage_id'],'stage_hash':digest(stage),'methodology_hash':digest(bindings),
        'route_receipt_hash':digest(receipt),'nonce':nonce,'workflow':{
            'domain_skills':[{k:v for k,v in d.items() if k!='source_path'} for d in bindings['domain_skills']],
            'process_owner':{k:v for k,v in bindings['process_owner'].items() if k!='source_path'}},
        'objective':stage['objective'],'inputs':stage['inputs'],'allowed_paths':stage['allowed_paths'],'forbidden':stage['forbidden'],
        'domain_rules':bindings['domain_rules'],'output_contract':stage['output_contract'],
        'controller_checks':[{'id':c['id'],'expected_exit':c['expected_exit'],'pins_hash':digest(c['pins'])} for c in stage['checks']],
        'stop_conditions':stage['stop_conditions'],'mode':stage['mode'],'runtime_proof_requirements':[] if receipt['selected_route']=='deterministic' else PROOF_FIELDS,
        'executor_route':receipt['selected_route'],'selected_context':list(selected_context),
        'execution':execution if execution is not None else {
            'attempt':1,'baseline_tree_hash':digest({i['path']:i['sha256'] for i in stage['inputs']}),
            'current_input_hashes':{i['id']:i['sha256'] for i in stage['inputs']},
            'previous_evidence_hash':None,'correction_evidence':[]}}
    contract(packet,'worker-packet.schema.json')
    if len(canonical(packet))>64*1024:raise ContractError('worker_packet_budget')
    # Copy through canonical JSON: callers cannot mutate the original stage through packet aliases.
    return loads(canonical(packet))
