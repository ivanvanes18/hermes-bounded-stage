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


## 1.4 addition: `native_pins`, and what pinning actually binds

`adapter.native_pins` is an OPTIONAL array of at most four already-pinned absolute
argv items, declaring ADDITIONAL native executables beyond `argv[0]`. It is an
explicit declaration, never content sniffing, and it is honoured at BOTH enforcement
sites, because `stage_contracts.validate_pins` runs first and drives
`pinned_hash(..., executable=)`, while `pinned_runtime.bind_command` runs second and
drives the snapshot mode and the ELF check. Changing one site alone leaves a large
native ELF rejected as `source_size` before the other site is ever entered.

Its validation matrix is exactly two reason codes. `native_pin_shape`: not a list,
more than four entries, or duplicates. `native_pin_not_pinned`: an entry outside
`pins`, or `argv[0]` named again. There is deliberately no `native_pin_not_in_argv`:
the pre-existing `set(pins) == {absolute argv items}` check already forces every
entry to be an absolute argv item, so a membership test would be unreachable.
Absence, `null` and any falsy value collapse to the empty list, so every
pre-existing adapter spec behaves byte-identically.

Each declared object still gets the full treatment: SHA-256 pinned, `O_NOFOLLOW`
opened per path component, `\x7fELF` checked, `+x`/no-setuid checked, capped at
`MAX_EXECUTABLE` 256 MiB, copied into a sealed memfd (`SEAL|SHRINK|GROW|WRITE`),
re-hashed after sealing, and mode `0500`. This widens "may be a 256 MiB sealed
executable memfd" from 1 to at most 5 objects per command. It grants no new
privilege: the adapter could already exec arbitrary bytes, and pinning makes those
bytes MORE constrained than before.

Cost, stated accurately. A large native pin is copied into a memfd and hashed four
times per `run_command` — `validate_pins` at `harness_adapter.py:22`, `bind_command`'s
copy-hash, `bind_command`'s sealed re-hash, and `validate_pins` again at
`harness_adapter.py:60`. An attempt performs one probe `run_command` and one run
`run_command`, so eight passes and two memfd copies per attempt. The first
`validate_pins` runs before the timeout clock starts; the second and all of
`bind_command` run inside the `timeout_seconds` window. Not optimised in this slice.

**What pinning binds, and what it does not.** Pinning covers exactly the directly
bound objects named in the spec and nothing transitively. The Python standard
library, the dynamic loader, libc, `/proc`, the OS kernel, its sandboxing behaviour,
the network path to any provider, and the entire npm distribution and package
closure of any pinned tool — every file that the pinned ELF opens, maps, spawns or
downloads at runtime — are trusted external prerequisites, NOT identity-verified.
Pinning a native ELF proves which bytes execute as the entry point. It proves
nothing about what those bytes then read or execute. No recursive package-identity
claim is made or may be made.


## Codex app-server (`codex-cli 0.153.4`)

`scripts/luna_codex_adapter.py` is the first real-capable adapter. It drives the
installed `codex app-server` over stdio newline-delimited JSON-RPC and uses exactly
four client methods — `initialize`, `thread/start`, `turn/start`, `turn/interrupt` —
plus the one client notification the frozen `ClientNotification` union contains,
`initialized`, which is a mandatory part of the handshake on both the probe and the
run path.

Protocol shapes are frozen from the installed CLI with
`codex app-server generate-json-schema --out <dir>`, which performs no model call.
Three shapes matter and are easy to get wrong: `turn/started` and `turn/completed`
carry the turn id at `params.turn.id` and have no `params.turnId` at all, while
`thread/tokenUsage/updated` is the only turn-scoped notification that does carry
`params.turnId`; reasoning effort is a `turn/start` parameter and a thread-level
config value, not a `thread/start` parameter; and `ReadOnlySandboxPolicy` requires
only `type`, so `networkAccess` may legitimately be absent on the wire.

All ten `ServerRequest` methods are declined and fail the run closed. The probe
issues `initialize` and `initialized` and nothing else. The full contract, the
verification table, the failure codes and the offline/live boundary are in
[luna adapter](luna-adapter.md).

Headroom note: the installed Codex ELF occupies 96.4 % of `MAX_EXECUTABLE`. The next
release will break pinning. Raising the cap is a separate reviewed change; this slice
only guarantees that the failure is the legible `executable_size` at both sites.
