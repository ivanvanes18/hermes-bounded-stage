"""Self-contained `hermes-executor-v1` adapter driving `codex app-server` over stdio.

It runs exactly one bounded read-only turn on gpt-5.6-luna at reasoning effort `max`
and returns honest identity / session / permission / usage evidence or nothing at all.

This file executes as a `/proc/self/fd/<n>` alias under `python3.13 -I -S`, so sibling
imports are impossible by construction. It imports the standard library only: never
Hermes core, never the loop, never the controller. It is a leaf -- it sees one stage
packet, and it never accepts its own result.

Identity is observed, never echoed: `proof.model` and `proof.effort` come from the
verified `ThreadStartResponse`, not from `requested_identity`. Missing identity,
session, terminal-status or usage evidence exits non-zero with empty stdout; a
measured zero is never fabricated.

Boundary. `reasoningEffort` is thread-level configured state. The 0.153.4 schema says
verbatim: "Current configured reasoning effort when loaded, otherwise the latest
persisted effort. Null when unset or unavailable. This is not per-turn execution
telemetry." `proof.effort` therefore attests the effort the thread was configured
with, not a measurement that the turn executed at that effort.

`permissions.network: false` describes worker tool permissions. It is NOT a claim that
this process, or Codex, cannot reach the network, and NOT an OS sandbox claim.
"""
from __future__ import annotations
import hashlib
import json
import os
import pwd
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time

PROTOCOL='hermes-executor-v1'
HARNESS='luna'
MODEL='gpt-5.6-luna'
EFFORT='max'
SANDBOX_MODE='read-only'
SANDBOX_POLICY={'type':'readOnly','networkAccess':False}
APPROVAL_POLICY='never'
APPROVALS_REVIEWER='user'
THREAD_SOURCE='user'
TURN_TRIGGER='hermes-bounded-stage'
EFFORT_CONFIG_KEY='model_reasoning_effort'
CLIENT_INFO={'name':'hermes-bounded-stage','version':'1.4.0','title':'Hermes bounded stage executor'}
MIN_CLI_VERSION=(0,153,0)
SUPPORTED_MODES=('read_only',)
EVIDENCE_KIND='live'
MAX_LINE=1024*1024
MAX_TOTAL_RX=32*1024*1024
MAX_NOTIFICATIONS=4096
MAX_PROMPT_BYTES=64*1024
VERSION_TIMEOUT=10
PROBE_ANCHOR_FLAG='--probe-anchor'
# The EXACT `codex --version` line the approved spec was built against. It is bound into
# argv, hence into `adapter_digest` and hence into the human approval, so the bytes that
# were measured and the CLI that answers must agree on one string -- not on an ordering.
EXPECTED_VERSION_FLAG='--expected-codex-version'
PROBE_ANCHOR_STUB={'purpose':'hermes-luna-probe-anchor','schema_version':1}
PROBE_ANCHOR_MAX_BYTES=4096
# The one monotonic deadline shared across a whole JSON-RPC session, kept below the
# adapter `timeout_seconds` cap of 120 so a clean refusal normally precedes the
# controller's hard kill. The controller's timeout remains the outer bound.
SESSION_TIMEOUT=110
# Defensive bound on the controller request. The controller already caps its own
# stdin at 128 KiB; this adapter does not rely on that.
MAX_REQUEST_BYTES=256*1024
INTERRUPT_GRACE=2.0
SHUTDOWN_GRACE=5.0
_FD_ALIAS=re.compile(r'^/proc/self/fd/(\d+)$')
_VERSION_LINE=re.compile(r'^codex-cli\s+(\d+)\.(\d+)\.(\d+)$')


class _Fail(Exception):
    """Public reason code only; never the rejected payload."""


class _Eof(Exception):
    """The peer closed its stream. Evaluated against the evidence gathered so far."""


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate_json_key')
        result[key]=value
    return result


def _nonfinite(_):raise ValueError('nonfinite_json')


def canonical(value):
    """Byte-identical to `schema_validation.canonical`; re-implemented, never imported."""
    try:
        return json.dumps(value,sort_keys=True,ensure_ascii=False,
                          separators=(',',':'),allow_nan=False).encode('utf-8')
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise _Fail('non_json_value') from None


def digest(value):return hashlib.sha256(canonical(value)).hexdigest()


def _loads(raw):
    try:return json.loads(raw.decode('utf-8'),object_pairs_hook=_pairs,parse_constant=_nonfinite)
    except (ValueError,UnicodeError,RecursionError):return None


def _fd_aliases(argv):
    """Fds referenced by `/proc/self/fd/<n>` argv items.

    Without this, CPython closes the inherited descriptors immediately before execv and
    the Codex alias no longer resolves. This is the single most failure-prone line here.
    """
    found={}
    for item in argv:
        match=_FD_ALIAS.match(item) if isinstance(item,str) else None
        if match:found[int(match.group(1))]=None
    return tuple(found)


def _split_argv(argv):
    """Enforce the §3.1 layout: Codex alias at argv[1], then the version and anchor pairs.

    Both trailing pairs are mandatory. A spec that omits either is malformed rather than
    permissive -- there is no "unbound version" mode and no stray argument is tolerated.
    """
    if len(argv)<6 or argv[-2]!=PROBE_ANCHOR_FLAG or argv[-4]!=EXPECTED_VERSION_FLAG:
        raise _Fail('argv_shape')
    codex_alias=argv[1];anchor=argv[-1];expected_version=argv[-3]
    if not isinstance(codex_alias,str) or not codex_alias.startswith('/'):raise _Fail('argv_shape')
    if not isinstance(anchor,str) or not anchor.startswith('/'):raise _Fail('argv_shape')
    if not isinstance(expected_version,str) or not _VERSION_LINE.match(expected_version):
        raise _Fail('argv_shape')
    prefix=tuple(argv[2:-4])
    if PROBE_ANCHOR_FLAG in prefix or EXPECTED_VERSION_FLAG in prefix:raise _Fail('argv_shape')
    return codex_alias,prefix,expected_version,anchor


def _validate_anchor(path):
    """Validate, then discard. A stray trailing argument is never silently tolerated."""
    try:
        info=os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size>PROBE_ANCHOR_MAX_BYTES:
            raise _Fail('probe_anchor_invalid')
        with open(path,'rb') as handle:raw=handle.read(PROBE_ANCHOR_MAX_BYTES+1)
    except OSError:
        raise _Fail('probe_anchor_invalid') from None
    if len(raw)>PROBE_ANCHOR_MAX_BYTES or _loads(raw)!=PROBE_ANCHOR_STUB:
        raise _Fail('probe_anchor_invalid')


def _child_env():
    """A literal dict derived from the passwd database. Nothing is inherited.

    No Hermes, gateway, GitHub, cloud, proxy or provider variable is ever forwarded.
    """
    try:home=pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:raise _Fail('home_unavailable') from None
    if not (home and os.path.isabs(home) and os.path.isdir(home)):raise _Fail('home_unavailable')
    return {'PATH':os.defpath,'HOME':home,'CODEX_HOME':os.path.join(home,'.codex'),
            'LANG':'C.UTF-8','LC_ALL':'C.UTF-8','TERM':'dumb',
            'TMPDIR':tempfile.mkdtemp(prefix='hermes-luna-')}


def _text(value):
    if isinstance(value,str):return value
    if value is None:return ''
    if isinstance(value,(int,float)) and not isinstance(value,bool):return repr(value)
    return canonical(value).decode('utf-8')


def _prompt(packet):
    """Deterministic, packet-only.

    Never included: nonce, packet_hash, route_receipt_hash, stage_hash, methodology_hash,
    route ids, workflow source paths, or anything from this process's own environment.
    """
    lines=['You are executing exactly one bounded, read-only stage turn.',
           'Read only. Modify nothing, create nothing, delete nothing.',
           'Do not escalate through a shell and do not request any approval.',
           'Any approval or tool-permission request ends this turn with a refusal.',
           'Exactly one turn: report your findings as the final message, then stop.','',
           'Objective: '+_text(packet.get('objective')),
           'Mode: '+_text(packet.get('mode'))]
    allowed=packet.get('allowed_paths') or []
    lines.append('Writable paths: '+(', '.join(_text(x) for x in allowed) if allowed else 'none'))
    for label,values in (('Forbidden',packet.get('forbidden')),
                         ('Stop conditions',packet.get('stop_conditions'))):
        lines.append(label+':')
        lines.extend('- '+_text(value) for value in (values or []))
    rules=[r for r in (packet.get('domain_rules') or []) if isinstance(r,dict)]
    if rules:
        lines.append('Domain rules:')
        lines.extend('- ['+_text(r.get('skill'))+'/'+_text(r.get('rule_id'))+'] '+_text(r.get('text'))
                     for r in rules)
    contract=packet.get('output_contract') or {}
    lines.append('Required output paths:')
    lines.extend('- '+_text(value) for value in (contract.get('required_paths') or []))
    lines.append('Inputs:')
    for item in packet.get('inputs') or []:
        if isinstance(item,dict):
            lines.append('- '+_text(item.get('id'))+' '+_text(item.get('path'))+
                         ' sha256='+_text(item.get('sha256')))
    context=[c for c in (packet.get('selected_context') or []) if isinstance(c,dict)]
    if context:
        lines.append('Selected context:')
        for item in context:
            lines.append('- '+_text(item.get('id'))+' source_sha256='+_text(item.get('source_sha256')))
            lines.append(_text(item.get('text')))
    execution=packet.get('execution') or {}
    lines.append('Attempt: '+_text(execution.get('attempt')))
    evidence=execution.get('correction_evidence') or []
    if evidence:
        lines.append('Correction evidence:')
        lines.extend('- '+_text(item) for item in evidence)
    prompt='\n'.join(lines)
    if len(prompt.encode('utf-8'))>MAX_PROMPT_BYTES:raise _Fail('prompt_budget')
    return prompt


class _Rpc:
    """Newline-delimited JSON-RPC over the child's stdio, under one shared deadline.

    Driven by `selectors` so a chatty server cannot deadlock the writer. The receive
    bounds here are the ADAPTER's budget; the controller's 128 KiB stdout budget is a
    separate, controller-side limit that §3.10 output discipline never approaches.
    """

    def __init__(self,proc,deadline):
        self.proc=proc;self.deadline=deadline
        self.reader=proc.stdout.fileno();self.writer=proc.stdin.fileno()
        os.set_blocking(self.reader,False);os.set_blocking(self.writer,False)
        self.buffer=b'';self.total=0;self.notifications=0;self.identifier=0
        self.selector=selectors.DefaultSelector()
        self.selector.register(self.reader,selectors.EVENT_READ,'read')

    def close(self):
        try:self.selector.close()
        except OSError:pass

    def _remaining(self):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise _Fail('rpc_timeout')
        return remaining

    def _absorb(self):
        chunk=os.read(self.reader,65536)
        if not chunk:raise _Eof()
        self.total+=len(chunk)
        if self.total>MAX_TOTAL_RX:raise _Fail('rx_budget')
        self.buffer+=chunk

    def _read_line(self):
        while True:
            index=self.buffer.find(b'\n')
            if index>=0:
                line=self.buffer[:index];self.buffer=self.buffer[index+1:]
                if len(line)>MAX_LINE:raise _Fail('rx_budget')
                return line
            if len(self.buffer)>MAX_LINE:raise _Fail('rx_budget')
            for key,_ in self.selector.select(min(self._remaining(),.1)):
                if key.data=='read':self._absorb()

    def _write(self,obj):
        data=canonical(obj)+b'\n'
        view=memoryview(data);offset=0
        self.selector.register(self.writer,selectors.EVENT_WRITE,'write')
        try:
            while offset<len(data):
                for key,_ in self.selector.select(min(self._remaining(),.1)):
                    if key.data=='write':
                        try:offset+=os.write(self.writer,view[offset:offset+65536])
                        except BrokenPipeError:raise _Eof() from None
                    else:self._absorb()
        finally:
            try:self.selector.unregister(self.writer)
            except (KeyError,ValueError):pass

    def next_message(self):
        while True:
            line=self._read_line().strip()
            if not line:continue
            message=_loads(line)
            if type(message) is not dict:raise _Fail('rpc_frame')
            return message

    def count_notification(self,message,state):
        self.notifications+=1
        if self.notifications>MAX_NOTIFICATIONS:raise _Fail('rx_budget')
        if state is not None:state.observe(message)

    def notify(self,method):self._write({'jsonrpc':'2.0','method':method})

    def request(self,method,params,state=None):
        self.identifier+=1;identifier=self.identifier
        self._write({'jsonrpc':'2.0','id':identifier,'method':method,'params':params})
        while True:
            message=self.next_message()
            if 'method' in message:
                if message.get('id') is not None:self.decline(message)
                self.count_notification(message,state);continue
            if message.get('id')!=identifier:raise _Fail('rpc_frame')
            if 'error' in message:raise _Fail('rpc_error')
            if 'result' not in message:raise _Fail('rpc_frame')
            return message['result']

    def decline(self,message):
        """A read-only single-turn bounded run has no legitimate reason to be asked."""
        try:self._write({'jsonrpc':'2.0','id':message.get('id'),
                         'error':{'code':-32001,'message':'approval_declined'}})
        except (_Fail,_Eof):pass
        raise _Fail('server_request_declined')

    def interrupt(self,thread_id,turn_id):
        self.identifier+=1;identifier=self.identifier;saved=self.deadline
        self.deadline=time.monotonic()+INTERRUPT_GRACE
        try:
            self._write({'jsonrpc':'2.0','id':identifier,'method':'turn/interrupt',
                         'params':{'threadId':thread_id,'turnId':turn_id}})
            while True:
                message=self.next_message()
                if 'method' not in message and message.get('id')==identifier:return
        except Exception:return
        finally:self.deadline=saved


class _Turn:
    """Notification scoping. The shapes differ, and reading the wrong field never matches.

    `turn/started` and `turn/completed` carry the turn id at `params.turn.id`;
    ONLY `thread/tokenUsage/updated` carries `params.turnId`.
    """

    def __init__(self,thread_id):
        self.thread_id=thread_id;self.turn_id=None
        self.terminal=None;self.usage=None;self.error=False;self.dropped=0

    def _mine(self,params):
        return params.get('threadId')==self.thread_id

    def observe(self,message):
        method=message.get('method');params=message.get('params')
        if type(params) is not dict:self.dropped+=1;return
        if method=='error':
            scope=params.get('threadId')
            if scope is None or scope==self.thread_id:self.error=True
            else:self.dropped+=1
            return
        if method=='thread/started':
            thread=params.get('thread')
            if not (type(thread) is dict and thread.get('id')==self.thread_id):self.dropped+=1
            return
        if method in ('turn/started','turn/completed'):
            turn=params.get('turn')
            if not (self._mine(params) and type(turn) is dict and
                    self.turn_id is not None and turn.get('id')==self.turn_id):
                self.dropped+=1;return
            if method=='turn/completed':self.terminal=turn.get('status')
            return
        if method=='thread/tokenUsage/updated':
            if self._mine(params) and self.turn_id is not None and params.get('turnId')==self.turn_id:
                self.usage=params.get('tokenUsage')
            else:self.dropped+=1
            return
        self.dropped+=1


def _spawn(codex_alias,prefix,subcommand,*,cwd,env):
    """The child deliberately stays inside the harness process group.

    Creating a new session here would orphan it from `os.killpg` cleanup.
    """
    return subprocess.Popen(['codex',*prefix,*subcommand],executable=codex_alias,
                            pass_fds=_fd_aliases([codex_alias,*prefix]),cwd=cwd,
                            stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,env=env)


def _shutdown(proc):
    """Close stdin first and let the peer finish draining it before signalling.

    The handshake `initialized` notification carries no response, so tearing the child
    down straight after writing it would race the child's read of that very frame.
    """
    if proc is None:return
    try:
        if proc.stdin and not proc.stdin.closed:proc.stdin.close()
    except OSError:pass
    try:proc.wait(timeout=SHUTDOWN_GRACE);return _close_reader(proc)
    except subprocess.TimeoutExpired:pass
    try:proc.terminate()
    except OSError:pass
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:proc.kill()
        except OSError:pass
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:pass
    _close_reader(proc)


def _close_reader(proc):
    try:
        if proc.stdout and not proc.stdout.closed:proc.stdout.close()
    except OSError:pass


def _handshake(rpc,codex_home):
    """`initialize`, then the MANDATORY `initialized` notification. Probe and run alike."""
    result=rpc.request('initialize',{'clientInfo':dict(CLIENT_INFO)})
    if type(result) is not dict:raise _Fail('initialize_invalid')
    for key in ('codexHome','platformFamily','platformOs','userAgent'):
        if not isinstance(result.get(key),str) or not result[key]:raise _Fail('initialize_invalid')
    if result['platformOs']!='linux':raise _Fail('initialize_invalid')
    if result['codexHome']!=codex_home:raise _Fail('codex_home_mismatch')
    rpc.notify('initialized')
    return result


def _verify_thread_start(result,workspace):
    """Every row fails closed. Identity is read from the response, never from the request."""
    if type(result) is not dict:raise _Fail('thread_start_shape')
    thread=result.get('thread')
    if type(thread) is not dict:raise _Fail('thread_start_shape')
    if result.get('model')!=MODEL:raise _Fail('model_mismatch')
    if result.get('reasoningEffort')!=EFFORT:raise _Fail('effort_not_proven')
    if result.get('cwd')!=workspace:raise _Fail('cwd_mismatch')
    sandbox=result.get('sandbox')
    if type(sandbox) is not dict or sandbox.get('type')!='readOnly':raise _Fail('sandbox_mismatch')
    # `ReadOnlySandboxPolicy` requires only `type`; `networkAccess` carries "default": false
    # and may be absent on the wire. A default is not an observation, and `proof.permissions
    # .network` is asserted to the controller as False. There is no fallback here, no
    # inference from `sandbox.type`, and no flag that relaxes this.
    if 'networkAccess' not in sandbox or sandbox['networkAccess'] is not False:
        raise _Fail('sandbox_network_not_proven')
    if result.get('approvalPolicy')!=APPROVAL_POLICY:raise _Fail('approval_policy_mismatch')
    if result.get('approvalsReviewer')!=APPROVALS_REVIEWER:raise _Fail('reviewer_mismatch')
    # Thread.model / Thread.reasoningEffort / Thread.threadSource are optional and nullable
    # in 0.153.4 and required here: a thread that will not restate its own identity yields
    # no proof. A real server omitting any of them refuses the run rather than degrading it.
    if thread.get('threadSource')!=THREAD_SOURCE:raise _Fail('thread_source_mismatch')
    thread_id=thread.get('id');session_uuid=thread.get('sessionId')
    if not (isinstance(thread_id,str) and thread_id and
            isinstance(session_uuid,str) and session_uuid):
        raise _Fail('session_binding_missing')
    if thread.get('model')!=MODEL or thread.get('reasoningEffort')!=EFFORT:
        raise _Fail('thread_identity_mismatch')
    return thread_id,session_uuid,result['model'],result['reasoningEffort']


def _measured_usage(value):
    """From the LAST matching notification. Zeros are never fabricated."""
    if type(value) is not dict:raise _Fail('usage_evidence_missing')
    total=value.get('total')
    if type(total) is not dict:raise _Fail('usage_evidence_missing')
    usage={}
    for name,field in (('input_tokens','inputTokens'),('output_tokens','outputTokens')):
        raw=total.get(field)
        if type(raw) is bool or type(raw) not in (int,float):raise _Fail('usage_evidence_missing')
        number=int(raw)
        if number!=raw or not 0<=number<=1_000_000_000:raise _Fail('usage_evidence_missing')
        usage[name]=number
    return usage


def _check(state):
    if state.error:raise _Fail('server_error_notification')
    if state.terminal is not None and state.terminal!='completed':raise _Fail('turn_not_completed')


def _drain(rpc,state):
    """Terminates only when BOTH a matching terminal completion AND matching usage exist.

    Neither alone ends the loop, and their arrival order is not assumed in either
    direction. Deadline, EOF and budget exhaustion all resolve to a named refusal.
    """
    while True:
        _check(state)
        if state.terminal=='completed' and state.usage is not None:return
        try:message=rpc.next_message()
        except _Eof:break
        except _Fail as failure:
            if str(failure)!='rpc_timeout':raise
            break
        if 'method' not in message:continue
        if message.get('id') is not None:rpc.decline(message)
        rpc.count_notification(message,state)
    _check(state)
    if state.terminal!='completed':raise _Fail('turn_not_completed')
    raise _Fail('usage_evidence_missing')


def _cli_version(codex_alias,prefix,env,expected):
    """Exact equality with the version bound into argv. A minimum is not a binding.

    `MIN_CLI_VERSION` is retained as a floor below which no spec may be honoured even if
    an approval names it, but it can never widen the accepted set: equality is checked
    first and is the whole gate.
    """
    try:
        proc=subprocess.run(['codex',*prefix,'--version'],executable=codex_alias,
                            pass_fds=_fd_aliases([codex_alias,*prefix]),stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,
                            timeout=VERSION_TIMEOUT)
    except (OSError,subprocess.SubprocessError):raise _Fail('cli_version_unreadable') from None
    if proc.returncode!=0:raise _Fail('cli_version_unreadable')
    try:text=proc.stdout.decode('utf-8','replace').strip()
    except UnicodeError:raise _Fail('cli_version_unreadable') from None
    match=_VERSION_LINE.match(text)
    if not match:raise _Fail('cli_version_unreadable')
    if text!=expected:raise _Fail('cli_version_mismatch')
    if tuple(int(part) for part in match.groups())<MIN_CLI_VERSION:
        raise _Fail('cli_version_unsupported')
    return text


def probe(codex_alias,prefix,expected_version):
    """No model call: `initialize` plus the handshake notification, and nothing else.

    Probe identity is this adapter's own pinned constants plus a proven CLI capability
    check -- the honest bound. The RUN is what proves the model served the turn.
    """
    env=_child_env();proc=None;rpc=None
    try:
        _cli_version(codex_alias,prefix,env,expected_version)
        # cwd is inherited: the controller derives it from the pinned probe anchor's
        # directory, outside the repo, the workspace and any Codex package tree.
        proc=_spawn(codex_alias,prefix,['app-server'],cwd=None,env=env)
        rpc=_Rpc(proc,time.monotonic()+SESSION_TIMEOUT)
        _handshake(rpc,env['CODEX_HOME'])
    except _Eof:raise _Fail('rpc_eof') from None
    except OSError:raise _Fail('codex_unavailable') from None
    finally:
        if rpc is not None:rpc.close()
        _shutdown(proc)
        shutil.rmtree(env['TMPDIR'],ignore_errors=True)
    return {'protocol':PROTOCOL,'operation':'probe','ready':True,
            'identity':{'harness':HARNESS,'model':MODEL,'effort':EFFORT},
            'supported_modes':list(SUPPORTED_MODES),'evidence_kind':EVIDENCE_KIND}


def run(request,codex_alias=None,prefix=None,expected_version=None):
    packet=request.get('packet');workspace=request.get('workspace')
    if type(packet) is not dict or not isinstance(workspace,str) or not workspace:
        raise _Fail('request_shape')
    for field in ('mode','allowed_paths','nonce'):
        if field not in packet:raise _Fail('request_shape')
    if codex_alias is None:
        codex_alias,prefix,expected_version,_anchor=_split_argv(list(sys.argv))
    prompt=_prompt(packet)
    resolved=os.path.realpath(workspace)
    env=_child_env();proc=None;rpc=None;state=None;succeeded=False
    try:
        # The run path checks the version too. Readiness can be minutes old and the
        # executable behind the alias is only as trustworthy as the version it reports
        # at the moment the turn is about to start.
        _cli_version(codex_alias,prefix or (),env,expected_version)
        proc=_spawn(codex_alias,prefix or (),['app-server'],cwd=resolved,env=env)
        rpc=_Rpc(proc,time.monotonic()+SESSION_TIMEOUT)
        _handshake(rpc,env['CODEX_HOME'])
        # Effort is not a thread/start parameter in 0.153.4: it is a turn/start parameter
        # and a thread-level config value. The config key is not trusted -- a wrong key
        # degrades to `effort_not_proven` rather than a silent default-effort run.
        started=rpc.request('thread/start',{
            'model':MODEL,'cwd':resolved,'sandbox':SANDBOX_MODE,'approvalPolicy':APPROVAL_POLICY,
            'approvalsReviewer':APPROVALS_REVIEWER,'threadSource':THREAD_SOURCE,'ephemeral':True,
            'config':{EFFORT_CONFIG_KEY:EFFORT}})
        thread_id,session_uuid,model,effort=_verify_thread_start(started,resolved)
        state=_Turn(thread_id)
        turn=rpc.request('turn/start',{
            'threadId':thread_id,'input':[{'type':'text','text':prompt}],'effort':EFFORT,
            'model':MODEL,'cwd':resolved,'sandboxPolicy':dict(SANDBOX_POLICY),
            'approvalPolicy':APPROVAL_POLICY,'turnTrigger':TURN_TRIGGER},state)
        if type(turn) is not dict or type(turn.get('turn')) is not dict:raise _Fail('turn_start_shape')
        turn_id=turn['turn'].get('id')
        if not isinstance(turn_id,str) or not turn_id:raise _Fail('turn_start_shape')
        state.turn_id=turn_id
        # TurnStartResponse.turn.status (typically inProgress) is never trusted.
        _drain(rpc,state)
        usage=_measured_usage(state.usage)
        session_id='codex:'+thread_id+':'+session_uuid+':'+turn_id
        if not 1<=len(session_id)<=128:raise _Fail('session_id_budget')
        body={'protocol':PROTOCOL,'operation':'run','evidence_kind':EVIDENCE_KIND,
              'proof':{'harness':HARNESS,'model':model,'effort':effort,'session_id':session_id,
                       'permissions':{'mode':packet['mode'],'allowed_paths':packet['allowed_paths'],
                                      'network':False},
                       'packet_hash':digest(packet),'nonce':packet['nonce'],'exit_code':0},
              'usage':usage}
        succeeded=True
        return body
    except _Eof:raise _Fail('rpc_eof') from None
    except OSError:raise _Fail('codex_unavailable') from None
    finally:
        if rpc is not None:
            if not succeeded and state is not None and state.turn_id is not None:
                rpc.interrupt(state.thread_id,state.turn_id)
            rpc.close()
        _shutdown(proc)
        shutil.rmtree(env['TMPDIR'],ignore_errors=True)


def main(argv=None,stdin=None):
    """Exactly one JSON object on stdout on success; nothing at all on failure."""
    argv=list(sys.argv if argv is None else argv)
    try:
        codex_alias,prefix,expected_version,anchor=_split_argv(argv)
        # Validated through its own sealed alias, then dropped: never forwarded to Codex.
        _validate_anchor(anchor)
        stream=sys.stdin.buffer if stdin is None else stdin
        raw=stream.read(MAX_REQUEST_BYTES+1)
        if not isinstance(raw,bytes) or len(raw)>MAX_REQUEST_BYTES:raise _Fail('request_budget')
        request=_loads(raw)
        if type(request) is not dict or request.get('protocol')!=PROTOCOL:raise _Fail('request_shape')
        operation=request.get('operation')
        if operation=='probe':body=probe(codex_alias,prefix,expected_version)
        elif operation=='run':body=run(request,codex_alias,prefix,expected_version)
        else:raise _Fail('request_operation')
        sys.stdout.buffer.write(canonical(body)+b'\n');sys.stdout.buffer.flush()
        return 0
    except _Fail as failure:
        # Diagnostic only. The controller discards this at the OS level and never
        # persists it; a non-zero exit surfaces there as `worker_exit_failure`.
        sys.stderr.write(str(failure)+'\n');sys.stderr.flush()
        return 1
    except BaseException:
        sys.stderr.write('adapter_internal_error\n');sys.stderr.flush()
        return 1


if __name__=='__main__':raise SystemExit(main())
