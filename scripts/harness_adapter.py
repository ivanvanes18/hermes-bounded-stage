"""Pinned subprocess adapter boundary, independent of model-generated prose.

A real adapter must be separately reviewed against the installed executor skill
and extract actual metadata from its harness. This generic protocol runner does
not invent Claude/Codex flags or models. Bundled tests supply a synthetic adapter.
"""
from __future__ import annotations
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from stage_contracts import ContractError,canonical,digest,loads,validate_pins
from executor_routes import validate_registry
from pinned_runtime import bind_command
MAX_OUTPUT=128*1024
PROTOCOL='hermes-executor-v1'


def run_command(spec,*,cwd,input_data=b'',worker_root=None,timeout_seconds=None):
    validate_pins(spec,worker_root)
    timeout=min(spec['timeout_seconds'],timeout_seconds) if timeout_seconds is not None else spec['timeout_seconds']
    if timeout<=0:raise ContractError('deadline_exceeded')
    if len(input_data)>128*1024:raise ContractError('adapter_input_budget')
    env={'PATH':os.defpath,'LANG':'C.UTF-8','PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'}
    started=time.monotonic();proc=None;chunks=[];size=0
    try:
        with bind_command(spec,cwd) as (args,executable,descriptors):
            proc=subprocess.Popen(args,executable=executable,pass_fds=descriptors,
                                  cwd=str(cwd),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,env=env,start_new_session=True)
        # The controller's bounded packet is written while stdout is drained,
        # so a non-reading child cannot deadlock this function on stdin.
        os.set_blocking(proc.stdin.fileno(),False);os.set_blocking(proc.stdout.fileno(),False)
        pending=memoryview(input_data);offset=0
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout,selectors.EVENT_READ,'out')
            if pending:selector.register(proc.stdin,selectors.EVENT_WRITE,'in')
            else:proc.stdin.close()
            while selector.get_map():
                remaining=timeout-(time.monotonic()-started)
                if remaining<=0:raise ContractError('adapter_timeout')
                for key,_ in selector.select(min(remaining,.1)):
                    if key.data=='in':
                        try:count=os.write(proc.stdin.fileno(),pending[offset:offset+65536])
                        except BrokenPipeError:
                            selector.unregister(proc.stdin);proc.stdin.close();continue
                        offset+=count
                        if offset==len(pending):selector.unregister(proc.stdin);proc.stdin.close()
                    else:
                        chunk=os.read(proc.stdout.fileno(),65536)
                        if not chunk:selector.unregister(proc.stdout);proc.stdout.close();continue
                        size+=len(chunk)
                        if size>MAX_OUTPUT:raise ContractError('adapter_output_budget')
                        chunks.append(chunk)
        remaining=timeout-(time.monotonic()-started)
        if remaining<=0:raise ContractError('adapter_timeout')
        code=proc.wait(timeout=remaining)
        validate_pins(spec,worker_root)
        return {'exit_code':code,'stdout':b''.join(chunks),'elapsed_ms':int((time.monotonic()-started)*1000)}
    except subprocess.TimeoutExpired:raise ContractError('adapter_timeout') from None
    except OSError:raise ContractError('adapter_unavailable') from None
    finally:
        if proc is not None:
            # Kill descendants too; a finished wrapper may have left children.
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            try:proc.wait(timeout=2)
            except subprocess.TimeoutExpired:pass
            for stream in (proc.stdin,proc.stdout):
                if stream and not stream.closed:stream.close()


def readiness(stage,registry,*,evidence_mode):
    validate_registry(registry)
    records={}
    for route in registry['routes']:
        if route['kind']!='executor':continue
        spec=route['adapter']
        r={'ready':False,'route_hash':digest(route),'registry_hash':digest(registry),'stage_hash':digest(stage),
           'evidence_kind':spec['evidence_kind'] if spec else evidence_mode,
           'harness':route['harness'],'model':route['model'],'effort':route['effort'],
           'supported_modes':[],'adapter_digest':digest(spec) if spec else None,
           'reason':'disabled_or_unverified','expires_at':time.time()+300}
        records[route['route_id']]=r
        if route['route_id'] not in stage['executor_candidates'] or route['status']!='approved-for-pilot':continue
        if spec is None or spec['evidence_kind']!=evidence_mode:
            r['reason']='adapter_evidence_mismatch';continue
        try:
            request={'protocol':PROTOCOL,'operation':'probe','requested_identity':{k:route[k] for k in ('harness','model','effort')}}
            # Probe outside the worker tree; no prompt/history/auth environment.
            got=run_command(spec,cwd=Path(spec['argv'][-1]).parent,input_data=canonical(request),worker_root=stage['workspace'])
            if got['exit_code']!=0:raise ContractError('probe_failed')
            body=loads(got['stdout'])
            if not isinstance(body,dict) or set(body)!={'protocol','operation','ready','identity','supported_modes','evidence_kind'}:
                raise ContractError('probe_envelope')
            if body['protocol']!=PROTOCOL or body['operation']!='probe' or body['evidence_kind']!=evidence_mode or body['ready'] is not True:
                raise ContractError('probe_unready')
            if body['identity']!={k:route[k] for k in ('harness','model','effort')}:raise ContractError('identity_mismatch')
            modes=body['supported_modes']
            if not isinstance(modes,list) or not modes or len(modes)!=len(set(modes)) or not set(modes)<= {'read_only','bounded_write'}:
                raise ContractError('probe_modes')
            r.update(ready=True,supported_modes=modes,reason='ready')
        except (ContractError,TypeError,ValueError):r['reason']='probe_failed_or_mismatched'
    return records


def execute(route,packet,workspace,*,evidence_mode,timeout_seconds):
    spec=route['adapter']
    if spec is None or spec['evidence_kind']!=evidence_mode:raise ContractError('adapter_evidence_mismatch')
    request={'protocol':PROTOCOL,'operation':'run','workspace':str(workspace),'packet':packet,
             'requested_identity':{k:route[k] for k in ('harness','model','effort')}}
    result=run_command(spec,cwd=workspace,input_data=canonical(request),worker_root=workspace,timeout_seconds=timeout_seconds)
    if result['exit_code']!=0:raise ContractError('worker_exit_failure')
    body=loads(result['stdout'])
    if not isinstance(body,dict) or set(body)!={'protocol','operation','evidence_kind','proof','usage'}:
        raise ContractError('runtime_proof_mismatch')
    if body['protocol']!=PROTOCOL or body['operation']!='run' or body['evidence_kind']!=evidence_mode:
        raise ContractError('runtime_proof_mismatch')
    proof=body['proof'];usage=body['usage']
    if not isinstance(proof,dict) or set(proof)!=set(packet['runtime_proof_requirements']):raise ContractError('runtime_proof_mismatch')
    expected_permissions={'mode':packet['mode'],'allowed_paths':packet['allowed_paths'],'network':False}
    if (any(proof[k]!=route[k] for k in ('harness','model','effort')) or
        not isinstance(proof['session_id'],str) or not 1<=len(proof['session_id'])<=128 or
        proof['nonce']!=packet['nonce'] or proof['packet_hash']!=digest(packet) or
        type(proof['exit_code']) is not int or proof['exit_code']!=0 or proof['permissions']!=expected_permissions):
        raise ContractError('runtime_proof_mismatch')
    if not isinstance(usage,dict) or set(usage)!={'input_tokens','output_tokens'} or any(type(v) is not int or not 0<=v<=1_000_000_000 for v in usage.values()):
        raise ContractError('runtime_usage_mismatch')
    return {'proof':proof,'usage':usage,'elapsed_ms':result['elapsed_ms'],'evidence_kind':evidence_mode,
            'adapter_digest':digest(spec),'response_sha256':__import__('hashlib').sha256(result['stdout']).hexdigest()}
