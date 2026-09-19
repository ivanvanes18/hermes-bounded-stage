"""Build the PRIVATE Luna pilot registry, outside the repository. Not installed.

`tools/` is deliberately outside the skill bundle: nothing here ships, and nothing here
is reachable from the installed skill. `assets/executor-routes.json` is READ and never
written -- this tool appends one route to an in-memory copy and writes the result to a
private file that the existing `--registry / --registry-sha256` flags then point at.

Every derivable value in the human-written approval record is RECOMPUTED here and
compared field by field. On any mismatch the tool prints the mismatching field names
and exits non-zero WITHOUT writing the registry. Nothing is inferred, defaulted or
coerced, and no field is accepted on trust.

Three values are *measured*, not read from the record:

  * the Codex ELF digest, hashed from a descriptor this tool HOLDS open, so the bytes
    that were measured are the bytes the version probe then executes;
  * the exact `codex --version` line, obtained by running `/proc/self/fd/<n>` on that
    same held descriptor -- no model call, no `app-server`, no network. The exact line
    is bound into the adapter spec argv as `--expected-codex-version <line>`, hence into
    `adapter_digest`, hence into `approval_sha256`. A *minimum* version is not a binding;
    the adapter refuses anything that is not that exact string;
  * the SHA-256 of the pre-pilot evidence bundle named by `--evidence-bundle`, read from
    a held descriptor of a private regular file. `evidence_bundle_sha256` in the approval
    record must equal it, so the approval is bound to the evidence it was granted on and
    a stale or substituted bundle refuses before any registry exists.

The registry is published through `pilot_io.publish`: a nofollow dirfd walk from `/`, a
private final parent, `O_CREAT|O_EXCL` creation, a complete write loop, `fsync` of file
and parent, and a readback through a fresh descriptor proving the exact bytes, owner,
mode and inode. No pathname is ever overwritten. The probe anchor -- the fourth pin and
the final `argv` item -- is verified or created through that same boundary at mode 0400,
while the registry itself stays 0600.

The anchor's parent directory is a PRECONDITION, not a side effect. Create it before
running this tool, privately, and never let this tool create it for you:

    mkdir -p ~/.hermes/pilot/probe-anchor
    chmod 700 ~/.hermes/pilot ~/.hermes/pilot/probe-anchor

Usage:
    python3 -B tools/pilot_registry.py --codex <elf> --python <elf> --adapter <py> \\
            --probe-anchor <abs path> --evidence-bundle <abs path> --bindings-only
    python3 -B tools/pilot_registry.py --codex <elf> --python <elf> --adapter <py> \\
            --probe-anchor <abs path> --evidence-bundle <abs path> \\
            --approval <record.json> --out <registry.json>
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'scripts'))
sys.path.insert(0,str(REPO/'tools'))
import pilot_io                                                         # noqa: E402
from schema_validation import ContractError,canonical,digest,loads      # noqa: E402
from stage_contracts import contract                                    # noqa: E402
from executor_routes import validate_registry                           # noqa: E402

ROUTE_ID='luna_max_read_only'
MODEL='gpt-5.6-luna'
EFFORT='max'
HARNESS='luna'
SKILL='luna-task-routing'
MODE='read_only'
EVIDENCE_KIND='live'
TIMEOUT_SECONDS=120
PROBE_ANCHOR_FLAG='--probe-anchor'
EXPECTED_VERSION_FLAG='--expected-codex-version'
PROBE_ANCHOR_STUB={'purpose':'hermes-luna-probe-anchor','schema_version':1}
# Mirrors the adapter's own anchor bounds; restated because nothing under `tools/` may
# depend on the installed skill's runtime.
PROBE_ANCHOR_MAX_BYTES=4096
ANCHOR_MODE=pilot_io.READONLY_MODE
PROOF_FIELDS=['harness','model','effort','session_id','permissions','packet_hash','nonce','exit_code']
SHIPPED=REPO/'assets/executor-routes.json'
APPROVAL_KEYS={'schema_version','route_id','purpose','harness','model','effort','mode','evidence_kind',
    'scope','data_classification','cli_version','python_sha256','adapter_sha256','codex_sha256',
    'probe_anchor_path','probe_anchor_sha256','evidence_bundle_sha256','adapter_digest',
    'approved_by','approved_at','expires_at'}
CODEX_PACKAGE_MARKERS=('node_modules','@openai')


class Refusal(Exception):
    """Public reason only. Never echoes a path's contents."""


def sha256_file(path):
    hasher=hashlib.sha256()
    with open(path,'rb') as handle:
        for chunk in iter(lambda:handle.read(65536),b''):hasher.update(chunk)
    return hasher.hexdigest()


def absolute(path,label):
    value=Path(path)
    if not value.is_absolute():raise Refusal(label+'_not_absolute')
    resolved=value.resolve()
    if str(resolved)!=str(value):raise Refusal(label+'_not_canonical')
    return resolved


def literal_absolute(path,label):
    """Absolute, but deliberately NOT resolved.

    Resolving would follow the very symlinks `pilot_io`'s nofollow walk exists to refuse,
    and would turn a symlinked target into a silently accepted one. Used for paths that
    are not pins; pins keep `absolute()`, which additionally requires canonical form
    because the pin path itself is what `stage_contracts.validate_pins` re-opens.
    """
    value=Path(path).expanduser()
    if not value.is_absolute():raise Refusal(label+'_not_absolute')
    return value


def outside_repo(path,label):
    if path==REPO or path.is_relative_to(REPO):raise Refusal(label+'_inside_repo')
    return path


def prepare_anchor(path):
    """Publish the stub at 0400 when absent; otherwise prove it is EXACTLY those bytes.

    The anchor is the fourth pin and the final `argv` item, so it is inside the same
    trust boundary as the registry and goes through the same `pilot_io` code path -- not
    through `exists`, `read_bytes` and `os.chmod` on a pathname anyone could re-point.

    The parent must ALREADY exist as a directory owned by this uid with no group or other
    bits, reached by the component-wise nofollow dirfd walk. It is never created here,
    recursively or otherwise: `mkdir -p` would mean materialising -- and then writing a
    pinned object into -- a namespace nothing validated. An existing anchor is read back
    through a held descriptor and must be exactly the stub at exactly `0400`; an absent
    one is created descriptor-relative with `O_CREAT|O_EXCL`, written in full, fsynced,
    and read back by bytes, owner, mode and inode.
    """
    anchor=outside_repo(absolute(path,'probe_anchor'),'probe_anchor')
    if any(marker in anchor.parts for marker in CODEX_PACKAGE_MARKERS):
        raise Refusal('probe_anchor_inside_codex_package')
    expected=canonical(PROBE_ANCHOR_STUB)
    try:
        body,_=pilot_io.read_private_file(anchor,max_bytes=PROBE_ANCHOR_MAX_BYTES,
                                          expect_mode=ANCHOR_MODE)
    except pilot_io.PilotIoError as failure:
        # Absence is the ONLY condition that publishes. Every other refusal -- a missing
        # or non-private parent, a symlink, a special file, a wrong mode -- propagates.
        if str(failure)!='source_missing':raise
        pilot_io.publish(anchor,expected,mode=ANCHOR_MODE)
    else:
        if body!=expected:raise Refusal('probe_anchor_not_stub')
    return anchor


def measure_codex(codex_elf):
    """Hold the ELF open, hash the held bytes, then ask THOSE bytes for their version.

    The descriptor is closed only after the version has been observed, so there is no
    window in which the pathname could be re-pointed between the measurement and the
    observation. Returns `(sha256, cli_version)`.
    """
    fd,measured,_=pilot_io.hold_executable(codex_elf)
    try:
        return measured,pilot_io.probe_cli_version(pilot_io.alias(fd),pass_fds=(fd,))
    finally:os.close(fd)


def build_spec(python_elf,adapter_source,codex_elf,codex_sha256,cli_version,anchor):
    # Neither element of the version pair is an absolute path, so neither becomes a pin;
    # both are argv, so both are inside `adapter_digest`.
    argv=[str(python_elf),'-I','-S',str(adapter_source),str(codex_elf),
          EXPECTED_VERSION_FLAG,cli_version,PROBE_ANCHOR_FLAG,str(anchor)]
    pins={str(python_elf):sha256_file(python_elf),str(adapter_source):sha256_file(adapter_source),
          str(codex_elf):codex_sha256,str(anchor):sha256_file(anchor)}
    return {'protocol':'hermes-executor-v1','argv':argv,'pins':pins,
            'native_pins':[str(codex_elf)],'timeout_seconds':TIMEOUT_SECONDS,
            'evidence_kind':EVIDENCE_KIND}


def build_route(spec,approval_sha256):
    return {'route_id':ROUTE_ID,'kind':'executor','harness':HARNESS,'skill':SKILL,'model':MODEL,
            'effort':EFFORT,'mode':MODE,'roles':['review'],'max_risk':'low','max_complexity':'medium',
            'status':'approved-for-pilot','fallback':'owner','approval_sha256':approval_sha256,
            'write_benchmark_sha256':None,'required_runtime_proof':list(PROOF_FIELDS),'adapter':spec}


def _rfc3339(value,label,problems):
    try:
        parsed=datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
    except (AttributeError,ValueError):
        problems.append(label);return None
    if parsed.tzinfo is None:problems.append(label);return None
    return parsed.astimezone(datetime.timezone.utc)


def validate_approval(record,spec,route,anchor,anchor_sha,digests,cli_version):
    """§4.1, field by field. Unknown keys, missing keys, nulls and type mismatches all refuse.

    Every entry in `expected` is a value this tool computed or measured in this process.
    Nothing is read back out of the record and compared against itself.
    """
    if type(record) is not dict:raise Refusal('approval_not_object')
    problems=sorted(APPROVAL_KEYS^set(record))
    if problems:raise Refusal('approval_key_set:'+','.join(problems))
    if any(record[key] is None for key in record):
        raise Refusal('approval_null_value:'+','.join(sorted(k for k in record if record[k] is None)))
    expected={'schema_version':1,'route_id':ROUTE_ID,'harness':route['harness'],'model':route['model'],
              'effort':route['effort'],'mode':route['mode'],'evidence_kind':spec['evidence_kind'],
              'scope':'single-run','data_classification':'public-redacted','approved_by':'parent',
              'cli_version':cli_version,'python_sha256':digests['python'],
              'adapter_sha256':digests['adapter'],'codex_sha256':digests['codex'],
              'probe_anchor_path':str(anchor),'probe_anchor_sha256':anchor_sha,
              'evidence_bundle_sha256':digests['evidence_bundle'],'adapter_digest':digest(spec)}
    problems=[key for key,value in expected.items() if record[key]!=value]
    if not isinstance(record['purpose'],str) or not record['purpose'].strip():problems.append('purpose')
    # The anchor must be BOTH the --probe-anchor argument and the final argv item, and the
    # measured version must be the argv item immediately before that pair.
    if spec['argv'][-2:]!=[PROBE_ANCHOR_FLAG,record['probe_anchor_path']]:
        problems.append('probe_anchor_path')
    if spec['argv'][-4:-2]!=[EXPECTED_VERSION_FLAG,record['cli_version']]:
        problems.append('cli_version')
    now=datetime.datetime.now(datetime.timezone.utc)
    approved=_rfc3339(record['approved_at'],'approved_at',problems)
    expires=_rfc3339(record['expires_at'],'expires_at',problems)
    if approved is not None and approved>now:problems.append('approved_at')
    if expires is not None and (approved is None or expires<=approved or expires<=now):
        problems.append('expires_at')
    if problems:raise Refusal('approval_field_mismatch:'+','.join(sorted(set(problems))))
    return digest(record)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex',required=True)
    parser.add_argument('--python',dest='python_elf',required=True)
    parser.add_argument('--adapter',required=True)
    parser.add_argument('--probe-anchor',dest='probe_anchor',required=True)
    parser.add_argument('--evidence-bundle',dest='evidence_bundle',required=True,
                        help='private regular file whose digest the approval must name')
    parser.add_argument('--approval')
    parser.add_argument('--out')
    parser.add_argument('--bindings-only',action='store_true')
    args=parser.parse_args(argv)
    out=None
    try:
        if args.bindings_only==bool(args.out):raise Refusal('exactly_one_of_bindings_only_or_out')
        if not args.bindings_only and not args.approval:raise Refusal('approval_required')
        if args.out:
            # Validated first, so a target that can never be published refuses before the
            # ~247 MiB measurement rather than after it.
            out=outside_repo(literal_absolute(args.out,'out'),'out')
            pilot_io.preflight_target(out)
        codex=absolute(args.codex,'codex');python_elf=absolute(args.python_elf,'python')
        adapter=absolute(args.adapter,'adapter');anchor=prepare_anchor(args.probe_anchor)
        for path,label in ((python_elf,'python'),(adapter,'adapter')):
            if not path.is_file():raise Refusal(label+'_missing')
        bundle=outside_repo(literal_absolute(args.evidence_bundle,'evidence_bundle'),
                            'evidence_bundle')
        _,evidence_sha256=pilot_io.read_private_file(bundle)
        codex_sha256,cli_version=measure_codex(codex)
        spec=build_spec(python_elf,adapter,codex,codex_sha256,cli_version,anchor)
        digests={'python':spec['pins'][str(python_elf)],'adapter':spec['pins'][str(adapter)],
                 'codex':codex_sha256,'evidence_bundle':evidence_sha256}
        anchor_sha=spec['pins'][str(anchor)]
        if args.bindings_only:
            # Print real values so the human fills the approval record from them, not from guesses.
            print(json.dumps({'mode':'bindings_only','written':False,'route_id':ROUTE_ID,
                'cli_version':cli_version,'python_sha256':digests['python'],
                'adapter_sha256':digests['adapter'],'codex_sha256':digests['codex'],
                'probe_anchor_path':str(anchor),'probe_anchor_sha256':anchor_sha,
                'evidence_bundle_path':str(bundle),'evidence_bundle_sha256':evidence_sha256,
                'adapter_digest':digest(spec),
                'argv':spec['argv'],'native_pins':spec['native_pins']},indent=1,sort_keys=True))
            return 0
        record=loads(Path(args.approval).expanduser().read_bytes())
        route=build_route(spec,'0'*64)
        approval_sha256=validate_approval(record,spec,route,anchor,anchor_sha,digests,cli_version)
        route=build_route(spec,approval_sha256)
        registry=loads(SHIPPED.read_bytes())
        shipped_ids={r['route_id'] for r in registry['routes']}
        if ROUTE_ID in shipped_ids:raise Refusal('route_already_shipped')
        before=canonical(registry['routes'])
        registry['routes'].append(route)
        if canonical(registry['routes'][:-1])!=before:raise Refusal('shipped_routes_mutated')
        contract(registry,'executor-registry.schema.json');validate_registry(registry)
        body=canonical(registry)
        written_sha256=pilot_io.publish(out,body)
        print(json.dumps({'path':str(out),'sha256':written_sha256,
            'route_id':ROUTE_ID,'adapter_digest':digest(spec),'approval_sha256':approval_sha256,
            'cli_version':cli_version,'codex_sha256':codex_sha256,
            'probe_anchor_path':str(anchor),'probe_anchor_sha256':anchor_sha,
            'evidence_bundle_path':str(bundle),'evidence_bundle_sha256':evidence_sha256},
            indent=1,sort_keys=True))
        return 0
    except (Refusal,ContractError,pilot_io.PilotIoError) as failure:
        print(json.dumps({'status':'refused','reason':str(failure),'written':False}))
        return 2
    except (OSError,ValueError,KeyError,TypeError) as failure:
        print(json.dumps({'status':'refused','reason':type(failure).__name__,'written':False}))
        return 2


if __name__=='__main__':raise SystemExit(main())
