"""Additive staged-routing CLI. Never installs, promotes, or changes Hermes configuration."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys
import executor_runtime as runtime
import executor_routes as routes
import harness_adapter
from stage_contracts import ContractError,read_file,validate_stage
from schema_validation import canonical,digest,loads
from typed_jev import TypedJev,JevError,PURPOSES
class Parser(argparse.ArgumentParser):
    def error(self,message):raise ContractError('cli_arguments')

def _json(path):return loads(read_file(Path(path).absolute(),1024*1024))

def _providers(args):
    enabled=getattr(args,'allow_typesafe',False);path=getattr(args,'grants',None)
    if enabled!=bool(path):raise ContractError('explicit_network_flag_and_grants_required')
    if not enabled:return {}
    grants=_json(path)
    if type(grants) is not list or len(grants)>64:raise ContractError('grant_file_shape')
    return {purpose:TypedJev(purpose=purpose,grants=grants) for purpose in PURPOSES}

def main(argv=None):
    try:
        parser=Parser(description=__doc__);cmds=parser.add_subparsers(dest='command',required=True,parser_class=Parser)
        cmds.add_parser('route-doctor')
        for name in ('route-validate','route-init','route-request'):
            cmd=cmds.add_parser(name);cmd.add_argument('--stage',required=True);cmd.add_argument('--registry',required=True)
            cmd.add_argument('--registry-sha256',required=True,help='SHA-256 of the exact registry file bytes')
            if name!='route-validate':cmd.add_argument('--evidence-mode',choices=('synthetic','live'),required=True)
            if name=='route-init':cmd.add_argument('--run',required=True);cmd.add_argument('--gate-evidence')
        for name in ('route-advance','route-inspect','route-accept','route-transition'):
            cmd=cmds.add_parser(name);cmd.add_argument('--run',required=True)
            if name=='route-accept':cmd.add_argument('--approval',required=True)
            if name=='route-advance':cmd.add_argument('--fixed-route')
            if name in ('route-advance','route-transition'):
                cmd.add_argument('--allow-typesafe',action='store_true');cmd.add_argument('--grants')
            if name=='route-transition':
                cmd.add_argument('--proposed',choices=('complete','one_bounded_correction','return_to_owner','clarify','stop'))
                cmd.add_argument('--parent-authorized-correction',action='store_true');cmd.add_argument('--triage-category')
        args=parser.parse_args(argv)
        if args.command=='route-doctor':
            result={'status':'local_prerequisites_checked','candidate_version':'1.2.0','python':sys.version.split()[0],
                    'python_supported':sys.version_info>=(3,13),'platform':sys.platform,
                    'executables_present':{n:shutil.which(n) is not None for n in ('hermes','claude','codex','git')},
                    'network_checked':False,'live_readiness_claim':False,'active_profile_changed':False}
        elif args.command in ('route-validate','route-init','route-request'):
            stage=_json(args.stage);registry=routes.load_registry(Path(args.registry).absolute(),args.registry_sha256)
            validate_stage(stage);routes.validate_registry(registry)
            if args.command=='route-validate':result={'status':'valid','stage_hash':digest(stage),'registry_hash':digest(registry),'parent_acceptance':False}
            elif args.command=='route-init':
                result=runtime.initialize(stage,registry,Path(args.run).absolute(),evidence_mode=args.evidence_mode,
                                          gate_evidence=_json(args.gate_evidence) if args.gate_evidence else None)
            else:
                ready=harness_adapter.readiness(stage,registry,evidence_mode=args.evidence_mode)
                request=routes.request(stage,registry,ready)
                payload={'model':'jev-1.13.0',**request}
                result={'status':'prepared_not_sent','request':payload,'payload_sha256':digest(payload),'readiness':ready,'external_send':False}
        elif args.command=='route-advance':result=runtime.advance(args.run,fixed_route=args.fixed_route,providers=_providers(args))
        elif args.command=='route-inspect':result=runtime.inspect(args.run)
        elif args.command=='route-accept':result=runtime.accept(args.run,_json(args.approval))
        else:
            providers=_providers(args)
            # Separate-purpose grants are required; choose the matching typed client
            # inside runtime through this mapping, never reuse a routing permission.
            result=runtime.transition(args.run,proposed=args.proposed,providers=providers,
                                      parent_authorized_correction=args.parent_authorized_correction,triage_category=args.triage_category)
        if len(canonical(result))>1024*1024:raise ContractError('cli_output_budget')
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        return 20 if result.get('status') in ('needs_review','stopped') else 0
    except (ContractError,JevError,OSError,ValueError,TypeError,KeyError,IndexError):
        # Never echo potentially private arguments, paths, bodies or exception text.
        print('{"status":"blocked","reason":"invalid_or_unavailable_staged_contract"}')
        return 2
if __name__=='__main__':raise SystemExit(main())
