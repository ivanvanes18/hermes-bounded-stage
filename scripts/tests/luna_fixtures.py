"""Synthetic fixtures for the Luna Codex adapter. No credentials, no model call.

`FAKE_APP_SERVER` fakes the *server* and nothing else: it is a real subprocess
speaking real newline-delimited JSON-RPC over real pipes, reached through a real
sealed memfd exec. No part of `luna_codex_adapter` is stubbed, mocked or replaced.

What this proves is this adapter's protocol handling, its stdio transport and the
controller's validation of the resulting envelopes. It is NOT evidence about the
real Codex filesystem sandbox, about provider egress, or about what the real
`codex app-server` populates.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sys

SCRIPTS=Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:sys.path.insert(0,str(SCRIPTS))
import executor_routes as R
import routing_fixtures as F
import stage_contracts as C
from schema_validation import canonical,digest

ROUTE_ID='luna_max_read_only'
MODEL='gpt-5.6-luna'
EFFORT='max'
HARNESS='luna'
PROBE_ANCHOR_FLAG='--probe-anchor'
EXPECTED_VERSION_FLAG='--expected-codex-version'
# What the fake's `--version` prints. The spec binds this EXACT string, so a spec built
# for any other value must refuse -- a minimum-version check would not.
CLI_VERSION='codex-cli 0.153.4'
PROBE_ANCHOR_STUB={'purpose':'hermes-luna-probe-anchor','schema_version':1}
PROBE_ANCHOR_MAX_BYTES=4096
ADAPTER_SOURCE=SCRIPTS/'luna_codex_adapter.py'
PYTHON=Path(sys.executable).resolve()
MAX_OUTPUT=128*1024

# Every scenario the fake app-server understands. Selected by an argv token, because
# the controller child environment is a hard 4-key allowlist and cannot carry one.
SCENARIOS=('good','wrong_model','wrong_effort','null_effort','wrong_source','wrong_sandbox',
    'missing_network_access','workspace_write_sandbox','wrong_cwd','approval_policy_on_request',
    'wrong_reviewer','no_session_id','foreign_thread_events','foreign_turn_events','no_usage',
    'usage_wrong_turn','usage_before_completion','completion_before_usage','approval_request',
    'tool_call_request','turn_failed','turn_interrupted','no_terminal','server_error_notification',
    'hang','oversized_rpc_line','notification_flood','duplicate_json_keys','garbage_line',
    'orphan_grandchild','old_cli_version','unparseable_version','bad_initialize','wrong_codex_home')

# Refuse on the RUN path. `old_cli_version` / `unparseable_version` are probe-only:
# the run path never invokes `--version`, so they behave like `good` there.
RUN_NEGATIVE=('wrong_model','wrong_effort','null_effort','wrong_source','wrong_sandbox',
    'missing_network_access','workspace_write_sandbox','wrong_cwd','approval_policy_on_request',
    'wrong_reviewer','no_session_id','foreign_thread_events','foreign_turn_events','no_usage',
    'usage_wrong_turn','approval_request','tool_call_request','turn_failed','turn_interrupted',
    'no_terminal','server_error_notification','oversized_rpc_line','notification_flood',
    'duplicate_json_keys','garbage_line','bad_initialize','wrong_codex_home')

FAKE_APP_SERVER=r'''"""Synthetic Codex app-server. Fakes the SERVER only, never the adapter."""
import hashlib,json,os,subprocess,sys,time

SIDECAR=__SIDECAR__
SCENARIO=sys.argv[1] if len(sys.argv)>1 else 'good'
SUB=sys.argv[2] if len(sys.argv)>2 else ''

if SUB=='--version':
    print({'old_cli_version':'codex-cli 0.152.9',
           'unparseable_version':'codex build vNEXT'}.get(SCENARIO,'codex-cli 0.153.4'))
    sys.exit(0)

THREAD='01930000-0000-7000-8000-0000000a7e1d'
SESSION='01930000-0000-7000-8000-0000005e5510'
TURN='01930000-0000-7000-8000-0000000c0de0'
OTHER_THREAD='01930000-0000-7000-8000-00000000f0f0'
OTHER_TURN='01930000-0000-7000-8000-00000000fafa'
TOTAL={'inputTokens':1234,'outputTokens':56,'cachedInputTokens':7,
       'reasoningOutputTokens':8,'totalTokens':1290}
LAST={'inputTokens':12,'outputTokens':3,'cachedInputTokens':0,
      'reasoningOutputTokens':1,'totalTokens':15}


def inherited():
    """Every fd the adapter actually handed down, identified by content, not by number."""
    out=[]
    for name in sorted(os.listdir('/proc/self/fd')):
        path='/proc/self/fd/'+name
        try:target=os.readlink(path);info=os.stat(path)
        except OSError:continue
        entry={'fd':int(name),'target':target,'size':info.st_size,'sha256':None}
        if 0<info.st_size<=65536:
            try:
                with open(path,'rb') as handle:entry['sha256']=hashlib.sha256(handle.read()).hexdigest()
            except OSError:pass
        out.append(entry)
    return out


record={'scenario':SCENARIO,'methods':[],'initialized_seen_at_thread_start':None,
        'cwd':os.getcwd(),'exe':os.readlink('/proc/self/exe'),'env':dict(os.environ),
        'fds':inherited(),'server_requests':[],'declined':[],'params':{},
        'pid':os.getpid(),'grandchild':None}


def flush():
    temporary=SIDECAR+'.tmp'
    with open(temporary,'w') as handle:handle.write(json.dumps(record))
    os.replace(temporary,SIDECAR)


def send(obj):
    sys.stdout.write(json.dumps(obj)+'\n');sys.stdout.flush()


def raw(line):
    sys.stdout.write(line+'\n');sys.stdout.flush()


def note(method,params):send({'jsonrpc':'2.0','method':method,'params':params})


def usage(thread=THREAD,turn=TURN):
    note('thread/tokenUsage/updated',{'threadId':thread,'turnId':turn,
                                      'tokenUsage':{'last':LAST,'total':TOTAL}})


def completed(status='completed',thread=THREAD,turn=TURN):
    note('turn/completed',{'threadId':thread,'turn':{'id':turn,'items':[],'status':status}})


def initialize_result():
    if SCENARIO=='bad_initialize':return {'codexHome':os.environ.get('CODEX_HOME',''),'platformOs':'linux'}
    home=os.environ.get('CODEX_HOME','')
    if SCENARIO=='wrong_codex_home':home='/nonexistent/.codex'
    return {'codexHome':home,'platformFamily':'unix','platformOs':'linux',
            'userAgent':'codex-cli/0.153.4 (synthetic fake)'}


def thread_start_result(params):
    cwd=params.get('cwd')
    thread={'cliVersion':'0.153.4','createdAt':1758240000,'cwd':cwd,'ephemeral':True,'id':THREAD,
            'modelProvider':'openai','preview':False,'projectId':None,'sessionId':SESSION,
            'source':'user','status':'active','turns':[],'updatedAt':1758240000,
            'model':'gpt-5.6-luna','reasoningEffort':'max','threadSource':'user'}
    result={'approvalPolicy':'never','approvalsReviewer':'user','cwd':cwd,'model':'gpt-5.6-luna',
            'modelProvider':'openai','reasoningEffort':'max',
            'sandbox':{'type':'readOnly','networkAccess':False},'thread':thread}
    if SCENARIO=='wrong_model':result['model']='gpt-5.5';thread['model']='gpt-5.5'
    if SCENARIO=='wrong_effort':result['reasoningEffort']='high';thread['reasoningEffort']='high'
    if SCENARIO=='null_effort':result['reasoningEffort']=None;thread['reasoningEffort']=None
    if SCENARIO=='wrong_source':thread['threadSource']='api'
    # A wrong sandbox TYPE while networkAccess is correctly false. Deliberately distinct
    # from `missing_network_access`, which keeps the type and drops the field.
    if SCENARIO=='wrong_sandbox':result['sandbox']={'type':'dangerFullAccess','networkAccess':False}
    if SCENARIO=='workspace_write_sandbox':
        result['sandbox']={'type':'workspaceWrite','networkAccess':False,'writableRoots':[]}
    # Exactly what ReadOnlySandboxPolicy permits: required is ["type"] only and
    # networkAccess carries "default": false. A default is not an observation.
    if SCENARIO=='missing_network_access':result['sandbox']={'type':'readOnly'}
    if SCENARIO=='wrong_cwd':result['cwd']='/tmp';thread['cwd']='/tmp'
    if SCENARIO=='approval_policy_on_request':result['approvalPolicy']='on-request'
    if SCENARIO=='wrong_reviewer':result['approvalsReviewer']='auto_review'
    if SCENARIO=='no_session_id':thread['sessionId']=''
    return result


def spawn_grandchild():
    child=subprocess.Popen(['codex-grandchild','-I','-S','-c','import time;time.sleep(600)'],
                           executable='/proc/self/exe',stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    record['grandchild']=child.pid;flush()


def emit_turn_stream():
    note('thread/started',{'thread':{'id':THREAD,'sessionId':SESSION,'cwd':record['cwd'],
                                     'cliVersion':'0.153.4','createdAt':1758240000,'ephemeral':True,
                                     'modelProvider':'openai','preview':False,'projectId':None,
                                     'source':'user','status':'active','turns':[],'updatedAt':1758240000}})
    note('turn/started',{'threadId':THREAD,'turn':{'id':TURN,'items':[],'status':'inProgress'}})
    if SCENARIO=='foreign_thread_events':
        note('turn/started',{'threadId':OTHER_THREAD,'turn':{'id':TURN,'items':[],'status':'inProgress'}})
        usage(thread=OTHER_THREAD);completed(thread=OTHER_THREAD);flush();sys.exit(0)
    if SCENARIO=='foreign_turn_events':
        usage(turn=OTHER_TURN);completed(turn=OTHER_TURN);flush();sys.exit(0)
    if SCENARIO=='usage_wrong_turn':
        usage(turn=OTHER_TURN);completed();flush();sys.exit(0)
    if SCENARIO=='no_usage':completed();flush();sys.exit(0)
    if SCENARIO=='no_terminal':usage();flush();sys.exit(0)
    if SCENARIO=='turn_failed':completed(status='failed');flush();sys.exit(0)
    if SCENARIO=='turn_interrupted':completed(status='interrupted');flush();sys.exit(0)
    if SCENARIO=='server_error_notification':
        note('error',{'threadId':THREAD,'turnId':TURN,'willRetry':False,
                      'error':{'message':'synthetic scoped failure'}})
        return
    if SCENARIO in ('approval_request','tool_call_request'):
        method='execCommandApproval' if SCENARIO=='approval_request' else 'item/tool/call'
        request={'jsonrpc':'2.0','id':9001,'method':method,
                 'params':{'threadId':THREAD,'turnId':TURN,'command':['echo','synthetic']}}
        record['server_requests'].append(method);flush();send(request);return
    if SCENARIO=='notification_flood':
        # Correctly scoped, non-terminal, and more than MAX_NOTIFICATIONS of them.
        for index in range(4200):
            note('turn/plan/updated',{'threadId':THREAD,'turnId':TURN,'plan':[{'step':index}]})
        return
    if SCENARIO=='hang':
        time.sleep(600);return
    if SCENARIO=='completion_before_usage':
        # Terminal FIRST, usage afterwards, stream deliberately left open.
        completed();time.sleep(.05);usage();return
    # `good`, `usage_before_completion` and `orphan_grandchild`: usage then terminal.
    usage();completed()


initialized=False
flush()
while True:
    line=sys.stdin.readline()
    if not line:break
    line=line.strip()
    if not line:continue
    try:message=json.loads(line)
    except ValueError:continue
    method=message.get('method');identifier=message.get('id')
    if method is None:
        record['declined'].append(message);record['methods'].append('response:'+str(identifier));flush();continue
    record['methods'].append(method);flush()
    # Causal handshake: nothing but `initialize` is processed before `initialized` arrives.
    if method not in ('initialize','initialized') and not initialized:
        send({'jsonrpc':'2.0','id':identifier,'error':{'code':-32002,'message':'not_initialized'}});continue
    if method=='initialize':
        send({'jsonrpc':'2.0','id':identifier,'result':initialize_result()});continue
    if method=='initialized':
        initialized=True;flush();continue
    if method=='thread/start':
        record['initialized_seen_at_thread_start']=initialized
        record['params']['thread/start']=message.get('params');flush()
        if SCENARIO=='oversized_rpc_line':
            raw(json.dumps({'jsonrpc':'2.0','method':'turn/plan/updated',
                            'params':{'threadId':THREAD,'note':'x'*(1024*1024+64)}}));continue
        if SCENARIO=='duplicate_json_keys':
            raw('{"jsonrpc":"2.0","id":%s,"result":{"a":1},"result":{"a":2}}'%json.dumps(identifier));continue
        if SCENARIO=='garbage_line':
            raw('this line is not JSON at all');continue
        send({'jsonrpc':'2.0','id':identifier,'result':thread_start_result(message.get('params') or {})});continue
    if method=='turn/start':
        record['params']['turn/start']=message.get('params');flush()
        if SCENARIO=='orphan_grandchild':spawn_grandchild()
        send({'jsonrpc':'2.0','id':identifier,
              'result':{'turn':{'id':TURN,'items':[],'status':'inProgress'}}})
        emit_turn_stream();flush();continue
    if method=='turn/interrupt':
        send({'jsonrpc':'2.0','id':identifier,'result':{}});flush();continue
    send({'jsonrpc':'2.0','id':identifier,'error':{'code':-32601,'message':'method_not_found'}})
flush()
'''

OVERSIZED_STDOUT_ADAPTER=r'''"""Standalone oversized-stdout stub: exercises the CONTROLLER stdout budget only.

It never launches the fake app-server and never imports luna_codex_adapter, so the
128 KiB `adapter_output_budget` is observed as its own outcome rather than being
confused with the adapter's receive budget.
"""
import sys
sys.stdin.read()
sys.stdout.write('x'*(128*1024+1))
sys.stdout.flush()
'''


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fake_native(tmp):
    """A real ELF (+x) standing in for the Codex native executable."""
    target=Path(tmp)/'codex'
    if not target.exists():
        shutil.copyfile(PYTHON,target);os.chmod(target,0o755)
    return target


def fake_server(tmp):
    target=Path(tmp)/'fake_app_server.py'
    if not target.exists():
        target.write_text(FAKE_APP_SERVER.replace('__SIDECAR__',repr(str(Path(tmp)/'sidecar.json'))))
    return target


def sidecar(tmp):
    path=Path(tmp)/'sidecar.json'
    if not path.exists():return None
    return json.loads(path.read_text())


def pidfile_pids(tmp):
    data=sidecar(tmp) or {}
    return [x for x in (data.get('pid'),data.get('grandchild')) if x]


def probe_anchor(tmp,*,variant='valid'):
    """A directory holding ONLY the anchor, so the derived probe cwd is unambiguous."""
    directory=Path(tmp)/'probe-anchor';directory.mkdir(exist_ok=True)
    path=directory/'anchor.json'
    if path.exists():os.chmod(path,0o600);path.unlink()
    body={'valid':canonical(PROBE_ANCHOR_STUB),
          'wrong_json':canonical({'purpose':'something-else','schema_version':1}),
          'garbage':b'not json at all\n',
          'oversize':canonical(PROBE_ANCHOR_STUB)+b' '*PROBE_ANCHOR_MAX_BYTES}[variant]
    path.write_bytes(body);os.chmod(path,0o400)
    return path


def adapter_spec(tmp,scenario,*,timeout=10,anchor='valid',anchor_pair=True,
                 expected_version=CLI_VERSION,version_pair=True):
    """The invocation layout: Codex alias at argv[1], version pair then anchor pair last."""
    tmp=Path(tmp);native=fake_native(tmp);server=fake_server(tmp)
    argv=[str(PYTHON),'-I','-S',str(ADAPTER_SOURCE),str(native),'-I','-S',str(server),scenario]
    pins={str(PYTHON):sha(PYTHON),str(ADAPTER_SOURCE):sha(ADAPTER_SOURCE),
          str(native):sha(native),str(server):sha(server)}
    # Neither element of the version pair is an absolute path, so neither is a pin.
    if version_pair:argv+=[EXPECTED_VERSION_FLAG,expected_version]
    if anchor_pair:
        path=probe_anchor(tmp,variant=anchor)
        argv+=[PROBE_ANCHOR_FLAG,str(path)];pins[str(path)]=sha(path)
    return {'protocol':'hermes-executor-v1','argv':argv,'pins':pins,'native_pins':[str(native)],
            'timeout_seconds':timeout,'evidence_kind':'live'}


def oversized_stdout_spec(tmp):
    target=Path(tmp)/'oversized_stdout_adapter.py';target.write_text(OVERSIZED_STDOUT_ADAPTER)
    return {'protocol':'hermes-executor-v1','argv':[str(PYTHON),'-I','-S',str(target)],
            'pins':{str(PYTHON):sha(PYTHON),str(target):sha(target)},
            'timeout_seconds':10,'evidence_kind':'live'}


def stage(tmp):
    """Read-only review stage: allowed_paths empty, no write budget, no feature gates."""
    value=F.stage(tmp)
    value.update(stage_id='luna-readonly-stage',mode='read_only',role='review',allowed_paths=[],
                 objective='Review sample.py and report findings; change nothing.',
                 executor_candidates=[ROUTE_ID,'owner'],
                 forbidden=['No file may change.','No shell escalation.'],
                 stop_conditions=['Any file change.','Any approval request.'],
                 data_policy={'classification':'public-redacted','external_allowed':True},
                 limits={'max_jev_calls':0,'max_worker_calls':1,'max_seconds':300,'max_context_chars':4000})
    value['output_contract']={'required_paths':['sample.py'],'allow_delete':False,'max_changed_files':0}
    return value


def route(spec):
    return {'route_id':ROUTE_ID,'kind':'executor','harness':HARNESS,'skill':'luna-task-routing',
            'model':MODEL,'effort':EFFORT,'mode':'read_only','roles':['review'],'max_risk':'low',
            'max_complexity':'medium','status':'approved-for-pilot','fallback':'owner',
            'approval_sha256':'4'*64,'write_benchmark_sha256':None,
            'required_runtime_proof':list(C.PROOF_FIELDS),'adapter':spec}


def registry(spec,*,status='approved-for-pilot'):
    """The shipped registry plus one appended pilot route. The shipped file is never edited."""
    value=F.registry()
    value['routes']=[r for r in value['routes'] if r['route_id']!='fixture_worker']
    entry=route(spec);entry['status']=status
    if status!='approved-for-pilot':entry['approval_sha256']=None
    value['routes'].append(entry)
    return value


def ready(reg,stage_value):
    entry=next(r for r in reg['routes'] if r['route_id']==ROUTE_ID)
    return {ROUTE_ID:{'ready':True,'route_hash':digest(entry),'registry_hash':digest(reg),
        'stage_hash':digest(stage_value),'evidence_kind':'live','harness':HARNESS,'model':MODEL,
        'effort':EFFORT,'supported_modes':['read_only'],'adapter_digest':digest(entry['adapter']),
        'reason':'ready','expires_at':4_000_000_000.0}}


def packet(stage_value,reg,nonce='c'*32,selected_context=()):
    receipt=R.decide(stage_value,reg,ready(reg,stage_value),None,fixed_route=ROUTE_ID)
    return C.worker_packet(stage_value,receipt,nonce,selected_context)


def run_request(stage_value,packet_value):
    return {'protocol':'hermes-executor-v1','operation':'run','workspace':stage_value['workspace'],
            'packet':packet_value,'requested_identity':{'harness':HARNESS,'model':MODEL,'effort':EFFORT}}


def probe_request():
    return {'protocol':'hermes-executor-v1','operation':'probe',
            'requested_identity':{'harness':HARNESS,'model':MODEL,'effort':EFFORT}}


def direct_argv(spec):
    """The same argv WITHOUT fd aliasing, for observing the adapter's own reason codes.

    `harness_adapter` discards adapter stderr at the OS level, so the controller path
    can never show which clause refused. This runs the identical file through the
    identical layout with stderr captured; the controller path is asserted separately.
    """
    return list(spec['argv'])
