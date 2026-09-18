# Trusted executor adapter protocol

Protocol name: `hermes-executor-v1`. This is a generic pinned subprocess interface, not a shipped Claude/Codex integration. Exact installed executor-skill sources were not included in the supplied bundle. A real adapter is a remaining local integration/evidence task, not a live-ready claim.

## Admission

Registry stores role ID, executor skill name, exact harness/model/effort, compatible mode/risk/role, approval hash, write-benchmark hash when writing, and fixed adapter argv/pins. Synthetic adapters occur only in tests/evaluation; the shipped registry has no approved real model route. Human review must bind adapter implementation to the actual installed skill/version and verify transitive runtime dependencies as well as explicit pins.

Controller passes a bounded JSON request via stdin. No shell interpolation, free model-generated command, ambient provider credential environment, redirect/retry or raw stderr persistence is allowed. stdout must be one bounded JSON object. Process groups are terminated on timeout/exit cleanup. This is process control, not OS filesystem/network isolation.

## Read-only probe

Request fields: protocol, operation=`probe`, requested_identity={harness,model,effort}.
Response fields exactly: protocol, operation=`probe`, ready boolean, identity={harness,model,effort}, supported_modes=[read_only|bounded_write], evidence_kind=synthetic|live.

An adapter must derive the identity from reliable harness metadata. Echoing the requested model is not evidence. The controller can verify agreement, not independently authenticate a dishonest trusted adapter. Real probes therefore require separate review and a pinned executable implementation.

## Run

Request fields: protocol, operation=`run`, workspace, packet, requested_identity.
Response fields exactly: protocol, operation=`run`, evidence_kind, proof, usage.

proof fields exactly: harness, model, effort, session_id, permissions, packet_hash, nonce, exit_code. packet_hash is SHA-256 over canonical JSON (UTF-8, sorted keys, compact separators, no NaN). nonce is copied from the actual current packet; permission proof must equal {mode,allowed_paths,network:false}. `network:false` means worker tool/workload permissions, not a claim that an approved model provider can be invoked without transport. Provider egress requires separately approved public/synthetic input policy and OS/harness controls.

usage fields exactly: input_tokens/output_tokens, bounded nonnegative integers. Missing trustworthy usage cannot be fabricated as measured zero. The bundled synthetic process has zero inference and identifies that fact explicitly.

## Required work for a real adapter

Read the installed `claude-code`, `codex` or approved equivalent skill; freeze its version/source. Probe the installed CLI help/version and provider metadata without changing config/auth. Prove exact model and effort from actual execution receipts, permission isolation and session binding. Then run the same disposable fixture through the actual CLI. Do not assume aliases, subscription limits, flags or output shapes from this package. A target CLI unable to prove identity leaves its route disabled/unverified.

The adapter must not launch the complete Shaw workflow or accept its own result. Shaw remains the parent's process owner; only the current stage packet goes to the executor.


## Candidate.2 correction: executable and script identity

I-1: document/source reads stay bounded at 16 MiB. Pinned native executables
have a separate 256 MiB limit and are hashed in 64 KiB chunks. Total pinned
command bytes are limited to 512 MiB. No unlimited read was introduced.

I-3: every source path component is opened using directory descriptors and
O_NOFOLLOW. The runtime and every other pinned absolute argv item are copied,
verified, and kernel-sealed against writes, growth and truncation. Popen executes
the retained executable's /proc/self/fd alias and receives sealed fd aliases for
adapter/script arguments. These aliases identify immutable objects, not original
pathnames. Readiness uses the same runner. The original argv[0] remains only a
runtime-discovery/display name; it is not the executable argument to Popen.

Direct shebang executables are refused: use an explicitly pinned native ELF
interpreter plus a pinned script. Adapters must support fd-based __file__/argv
paths; do not silently fall back to the original script path. Libraries, dynamic
loader, interpreter packages, imports and procfs still require a trusted target
runtime and independent isolation review. This is not recursive pinning of every
transitive dependency, not network isolation and not an OS sandbox.

After execution, pins are checked again as defense in depth. Replacement or
in-place modification can therefore return command_pin_changed while the
substituted bytes have never executed. The child environment allowlist and
process-group termination behavior are preserved.
