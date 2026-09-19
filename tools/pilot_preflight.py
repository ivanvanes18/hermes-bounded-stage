"""Pre-pilot observation against the REAL installed Codex. No model call. Not installed.

Three no-model-call operations, all of which the live pilot in the plan's §10 depends on:

  (default)                   `initialize` + `account/rateLimits/read` -- clears the
                              usage-limit blocker before anything else is attempted.
  --dump-config-requirements  `configRequirements/read` -- confirms the accepted config
                              key names, in particular `model_reasoning_effort`.
  --observe-thread-start      `initialize` -> `initialized` -> `thread/start` with exactly
                              the adapter's run parameters, then closes the thread. It
                              issues NO `turn/start`, so a thread is created, no turn is
                              executed and no tokens are spent.

`--observe-thread-start` exits non-zero unless EVERY row below holds, and writes the raw
`ThreadStartResponse` either way -- the observation file goes into the evidence bundle
even when it records the refusal:

    sandbox.type            == 'readOnly'
    sandbox.networkAccess   present on the wire and exactly the JSON literal false
    reasoningEffort         == 'max', present and non-null
    thread.model            == 'gpt-5.6-luna', present and non-null
    thread.reasoningEffort  == 'max', present and non-null
    thread.threadSource     == 'user', present and non-null
    approvalPolicy          == 'never'
    approvalsReviewer       == 'user'

These are the same rows the adapter enforces at run time, and they are checked here by
importing the adapter's own client so the two cannot drift apart. Observing them first
means a refusal surfaces BEFORE any registry is written rather than as a consumed pilot
attempt. "Stop" means exactly that: no retry loop, no model switch, no weakening of the
check, no registry write. In particular, the schema `"default": false` for
`networkAccess` is never substituted for an observation.

The Codex executable is HELD, not named. `--codex` is opened with a component-wise
`O_NOFOLLOW` dirfd walk; the descriptor is `fstat`ed as a regular, non-setuid, owner-
executable native ELF, hashed through that descriptor, and then executed as
`/proc/self/fd/<n>` with `pass_fds=`. The version this tool reports and the `app-server`
it drives therefore come from the same inode the digest was taken of, and replacing the
pathname afterwards cannot substitute different bytes. `--expected-codex-sha256` binds
that measurement to a value supplied by the caller; the measured digest is reported
either way, including on a refusal.

`--out` is validated before anything is held or spawned and published through
`pilot_io.publish`: never an overwrite, always a readback.

This tool never edits `~/.codex/config.toml`, `~/.codex/auth.json`, the default profile,
or `assets/executor-routes.json`.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'scripts'))
sys.path.insert(0,str(REPO/'tools'))
import pilot_io                                                         # noqa: E402
import luna_codex_adapter as adapter                                    # noqa: E402

REQUIRED_ROWS=(
    ('sandbox.type','readOnly'),
    ('sandbox.networkAccess',False),
    ('reasoningEffort',adapter.EFFORT),
    ('thread.model',adapter.MODEL),
    ('thread.reasoningEffort',adapter.EFFORT),
    ('thread.threadSource',adapter.THREAD_SOURCE),
    ('approvalPolicy',adapter.APPROVAL_POLICY),
    ('approvalsReviewer',adapter.APPROVALS_REVIEWER),
)


class Refusal(Exception):
    """Public reason code only."""


def _dig(value,path):
    """Return (present, value). Absence is distinguished from a null, always."""
    missing=object()
    for part in path.split('.'):
        if type(value) is not dict or part not in value:return False,None
        value=value.get(part,missing)
    return True,value


def _session(codex,*,cwd=None):
    env=adapter._child_env()
    proc=adapter._spawn(str(codex),(),['app-server'],cwd=cwd,env=env)
    rpc=adapter._Rpc(proc,time.monotonic()+adapter.SESSION_TIMEOUT)
    return env,proc,rpc


def _close(env,proc,rpc):
    if rpc is not None:rpc.close()
    adapter._shutdown(proc)
    shutil.rmtree(env['TMPDIR'],ignore_errors=True)


def read_only_call(codex,method):
    """`initialize` + one no-model-call read. Never `thread/start`, never `turn/start`."""
    env,proc,rpc=_session(codex)
    try:
        info=adapter._handshake(rpc,env['CODEX_HOME'])
        return {'initialize':info,'method':method,'result':rpc.request(method,None)}
    finally:_close(env,proc,rpc)


def observe_thread_start(codex,workspace):
    """Create a thread with the adapter's exact run parameters. No turn is started."""
    resolved=os.path.realpath(workspace)
    env,proc,rpc=_session(codex,cwd=resolved)
    try:
        info=adapter._handshake(rpc,env['CODEX_HOME'])
        started=rpc.request('thread/start',{
            'model':adapter.MODEL,'cwd':resolved,'sandbox':adapter.SANDBOX_MODE,
            'approvalPolicy':adapter.APPROVAL_POLICY,'approvalsReviewer':adapter.APPROVALS_REVIEWER,
            'threadSource':adapter.THREAD_SOURCE,'ephemeral':True,
            'config':{adapter.EFFORT_CONFIG_KEY:adapter.EFFORT}})
        return info,started
    finally:_close(env,proc,rpc)


def evaluate(started):
    rows=[]
    for path,required in REQUIRED_ROWS:
        present,value=_dig(started,path)
        if path=='sandbox.networkAccess':
            # A missing key, null, 0 or '' all fail. A default is not an observation.
            ok=present and value is False
        else:
            ok=present and value==required and value is not None
        rows.append({'field':path,'required':required,'present':present,'observed':value,'ok':ok})
    return rows


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--codex',required=True,help='absolute path to the Codex native ELF')
    parser.add_argument('--expected-codex-sha256',dest='expected_codex_sha256',default=None,
                        help='refuse unless the HELD bytes hash to exactly this')
    parser.add_argument('--dump-config-requirements',action='store_true')
    parser.add_argument('--observe-thread-start',action='store_true')
    parser.add_argument('--workspace',default=None,help='cwd for --observe-thread-start')
    parser.add_argument('--out',default=None,help='write the raw observation here')
    args=parser.parse_args(argv)
    if args.dump_config_requirements and args.observe_thread_start:
        print(json.dumps({'status':'refused','reason':'one_operation_at_a_time'}));return 2
    measured={}

    def refuse(reason):
        print(json.dumps({'status':'refused','reason':reason,**measured},
                         indent=1,sort_keys=True))
        return 2

    out=Path(args.out).expanduser() if args.out else None
    try:
        # Nothing is held, measured or spawned until the destination is known to be
        # publishable. A doomed --out must not consume a Codex session.
        if out is not None:pilot_io.preflight_target(out)
        fd,codex_sha256,_=pilot_io.hold_executable(Path(args.codex).expanduser())
    except pilot_io.PilotIoError as failure:return refuse(str(failure))
    try:
        measured['codex_sha256']=codex_sha256
        if args.expected_codex_sha256 and args.expected_codex_sha256!=codex_sha256:
            raise Refusal('codex_digest_mismatch')
        codex=pilot_io.alias(fd)
        measured['codex_cli_version']=pilot_io.probe_cli_version(codex,pass_fds=(fd,))
        if args.observe_thread_start:
            workspace=args.workspace or os.getcwd()
            info,started=observe_thread_start(codex,workspace)
            rows=evaluate(started)
            passed=all(row['ok'] for row in rows)
            payload={'status':'observed' if passed else 'refused','no_model_call':True,
                     'turn_started':False,'initialize':info,'thread_start_response':started,
                     'rows':rows,'failing':[row['field'] for row in rows if not row['ok']],
                     **measured}
            if out is not None:
                # Kept whether it passed or refused; it is evidence either way.
                pilot_io.publish(out,json.dumps(payload,indent=1,sort_keys=True).encode('utf-8'))
            print(json.dumps(payload,indent=1,sort_keys=True))
            return 0 if passed else 2
        method='configRequirements/read' if args.dump_config_requirements else 'account/rateLimits/read'
        payload={'status':'read','no_model_call':True,**measured,**read_only_call(codex,method)}
        if out is not None:
            pilot_io.publish(out,json.dumps(payload,indent=1,sort_keys=True).encode('utf-8'))
        print(json.dumps(payload,indent=1,sort_keys=True))
        return 0
    except (Refusal,pilot_io.PilotIoError) as failure:return refuse(str(failure))
    except adapter._Fail as failure:return refuse(str(failure))
    except adapter._Eof:return refuse('rpc_eof')
    except (OSError,ValueError,KeyError,TypeError) as failure:
        return refuse(type(failure).__name__)
    finally:os.close(fd)


if __name__=='__main__':raise SystemExit(main())
