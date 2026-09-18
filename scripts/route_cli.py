"""Additive staged-routing CLI. Never installs, promotes, or changes Hermes configuration."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys
import bounded_loop as loop_controller
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

def _loop(args):
    """Loop-level parent actions. No auto-grant path: the CLI supplies no classifier."""
    if args.command in ('loop-validate','loop-init','loop-redirect'):
        envelope=_json(args.envelope)
        registry=routes.load_registry(Path(args.registry).absolute(),args.registry_sha256)
        if args.command=='loop-validate':
            loop_controller.validate_envelope(envelope)
            return {'status':'valid','loop_hash':digest(envelope),'registry_hash':digest(registry),
                    'stages':len(envelope['stages']),'parent_acceptance':False}
        gate=_json(args.gate_evidence) if args.gate_evidence else None
        if args.command=='loop-init':
            return loop_controller.initialize(envelope,registry,Path(args.loop).absolute(),
                evidence_mode=args.evidence_mode,gate_evidence=gate)
        return loop_controller.redirect(Path(args.loop).absolute(),Path(args.new_loop).absolute(),
            envelope,registry,approval=_json(args.approval),evidence_mode=args.evidence_mode,
            gate_evidence=gate)
    loop=Path(args.loop).absolute()
    if args.command=='loop-advance':
        providers=_providers(args)
        # --fixed-stage selects the pre-admitted stage; --fixed-route selects the
        # admitted executor inside it. Without a parent classifier the Jev
        # selection path fails closed, exactly as route-advance does.
        return loop_controller.advance(loop,fixed_stage=args.fixed_stage,fixed_route=args.fixed_route,
            jev=providers.get('stage_transition'),providers=providers)
    if args.command=='loop-checkpoint':
        return loop_controller.checkpoint(loop,args.decision,
            approval=_json(args.approval) if args.approval else None)
    if args.command=='loop-inspect':return loop_controller.inspect(loop)
    if args.command=='loop-recover':return loop_controller.recover(loop)
    return loop_controller.accept(loop,_json(args.approval))

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
        for name in ('loop-validate','loop-init','loop-redirect'):
            cmd=cmds.add_parser(name);cmd.add_argument('--envelope',required=True)
            cmd.add_argument('--registry',required=True)
            cmd.add_argument('--registry-sha256',required=True,help='SHA-256 of the exact registry file bytes')
            if name!='loop-validate':
                cmd.add_argument('--loop',required=True)
                cmd.add_argument('--evidence-mode',choices=('synthetic','live'),required=True)
                cmd.add_argument('--gate-evidence')
            if name=='loop-redirect':
                cmd.add_argument('--new-loop',required=True);cmd.add_argument('--approval',required=True)
        for name in ('loop-advance','loop-checkpoint','loop-inspect','loop-recover','loop-accept'):
            cmd=cmds.add_parser(name);cmd.add_argument('--loop',required=True)
            if name=='loop-advance':
                cmd.add_argument('--fixed-stage');cmd.add_argument('--fixed-route')
                cmd.add_argument('--allow-typesafe',action='store_true');cmd.add_argument('--grants')
            if name=='loop-checkpoint':
                cmd.add_argument('--decision',choices=('continue','stop'),required=True)
                cmd.add_argument('--approval')
            if name=='loop-accept':cmd.add_argument('--approval',required=True)
        args=parser.parse_args(argv)
        if args.command=='route-doctor':
            result={'status':'local_prerequisites_checked','candidate_version':'1.3.0','python':sys.version.split()[0],
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
        elif args.command.startswith('loop-'):result=_loop(args)
        else:
            providers=_providers(args)
            # Separate-purpose grants are required; choose the matching typed client
            # inside runtime through this mapping, never reuse a routing permission.
            result=runtime.transition(args.run,proposed=args.proposed,providers=providers,
                                      parent_authorized_correction=args.parent_authorized_correction,triage_category=args.triage_category)
        if len(canonical(result))>1024*1024:raise ContractError('cli_output_budget')
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        # A parent must look: needs_review / stopped / checkpoint_required. Completed
        # parent actions (redirected, loop_accepted, projection_*) are 0.
        return 20 if result.get('status') in ('needs_review','stopped','checkpoint_required') else 0
    except (ContractError,JevError,OSError,ValueError,TypeError,KeyError,IndexError):
        # Never echo potentially private arguments, paths, bodies or exception text.
        print('{"status":"blocked","reason":"invalid_or_unavailable_staged_contract"}')
        return 2
if __name__=='__main__':raise SystemExit(main())
