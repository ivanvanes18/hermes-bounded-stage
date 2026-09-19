# Luna Codex Executor Adapter — Vertical Slice 1 (shipped disabled)

> **Ownership note (agentic worker).** This plan is written to be executed by an agentic worker, not by a human reader skimming for intent. The worker owns the *execution* of the tasks below and nothing more: it implements exactly the named files and symbols, in the stated order, writing the listed tests RED before the corresponding implementation. It does not widen scope, invent additional modules, relax a contract to make a test pass, or mark a task complete on a partial result. Every non-goal in **Global Constraints** is binding. Architectural or scope ambiguity discovered mid-execution stops the task and returns to the parent with the facts — it is never resolved by guessing. Final acceptance of this work is parent-only; a green test suite is evidence, not acceptance. **The live pilot in §10 is not part of the implementation session** — it is a separate, human-initiated run after the blocker in §10.0 clears.

## Goal

Add the first **real-capable** `hermes-executor-v1` adapter: a self-contained pinned subprocess that drives the installed `codex app-server` over stdio JSON-RPC, runs exactly one bounded read-only turn on `gpt-5.6-luna` at reasoning effort `max`, and returns honest identity / session / permission / usage evidence or nothing at all.

The slice is complete when:

1. a strict synthetic fake-app-server proves every contract clause — happy path, each identity mismatch, the mandatory `initialized` handshake (the fake **refuses `thread/start`** until it has arrived, so the test is causal rather than declarative), Codex-0.153.4-exact notification scoping, **both** terminal-vs-usage arrival orders as two explicit scenarios, foreign events, missing usage, an **absent `sandbox.networkAccess`** as a scenario distinct from a wrong `sandbox.type`, approvals, timeout, the probe-anchor argv shape and stub validation, the adapter's **receive** budget and the controller's **stdout** budget as two separately observed refusals, child-environment filtering, process-group cleanup, and the nonce / packet-hash / permissions proof — through the **real** `harness_adapter` code path; and
2. an offline probe proves identity and CLI capability against a **fake Codex executable**, with no model call anywhere in the suite; and
3. `assets/executor-routes.json` is byte-identical to `008e78f` and every real executor route is still `unverified`, `adapter:null`, `model:null`.

**What the synthetic suite does and does not prove** (restated in §8.4 and in the pilot `boundary.md`): the fake app-server is evidence about *this adapter's* protocol handling, its stdio transport, and the controller's validation of the resulting envelopes. It is **not** evidence about the real Codex filesystem sandbox, about provider egress, or about what the real `codex app-server` populates. The workspace tree digest taken before and after a run is **artifact-integrity evidence only** — it shows this adapter produced no file change on the paths measured; it is not a sandbox claim and not a containment proof.

Live admission is explicitly *not* part of this slice. It happens once, afterwards, through a private out-of-tree pilot registry (§10), and only on the strength of a named evidence bundle.

## Architecture

```
                 parent (owns stage, budgets, route choice, final acceptance)
                    ▼
  executor_runtime.advance()                                    ← UNCHANGED
     │
     ├── harness_adapter.readiness()   probe   ← UNCHANGED
     └── harness_adapter.execute()     run     ← UNCHANGED
            │
            │  bind_command()  ← pinned_runtime.py    [native_pins: TWO sites]
            │  validate_pins() ← stage_contracts.py   [native_pins: TWO sites]
            │     python3.13 ELF ─────────► Popen(executable='/proc/self/fd/A')
            │     luna_codex_adapter.py ──► argv[3] = '/proc/self/fd/B'   (sealed, 0400)
            │     codex native ELF ───────► argv[4] = '/proc/self/fd/C'   (sealed, 0500)  ◄── NEW
            │     probe anchor file ─────► argv[-1] = '/proc/self/fd/E'  (sealed, 0400)  ◄── NEW
            ▼
  scripts/luna_codex_adapter.py                                 ← NEW, stdlib only
     │   env built from pwd.getpwuid(os.getuid()).pw_dir — never inherited
     │   Popen('/proc/self/fd/C', ['codex','app-server'], pass_fds=(C,))
     ▼
  codex app-server  (stdio, newline-delimited JSON-RPC)
     ├── initialize                      → codexHome / platformFamily / platformOs / userAgent
     ├── initialized  (notification)     → MANDATORY handshake completion, no response
     ├── thread/start                    → ThreadStartResponse: model, reasoningEffort,
     │                                     cwd, sandbox{type,networkAccess}, approvalPolicy,
     │                                     approvalsReviewer, thread{id,sessionId,threadSource,
     │                                     model,reasoningEffort}
     ├── turn/start                      → TurnStartResponse{turn:{id,...}}
     ├── notifications (scoped to OUR ids only — SHAPES DIFFER, see §3.6)
     │     turn/started      params {threadId, turn{id}}      ← turn id at params.turn.id
     │     turn/completed    params {threadId, turn{id,status}}← turn id at params.turn.id
     │     thread/tokenUsage/updated
     │                       params {threadId, turnId, tokenUsage{last,total}}
     │                                                        ← turn id at params.turnId
     │     drain until BOTH matching terminal completion AND matching usage
     └── ANY ServerRequest (approval / tool / elicitation)
                                         → JSON-RPC error, turn/interrupt, FAIL CLOSED
```

- **The adapter is a leaf.** It never imports Hermes core, never sees the loop, never accepts its own result. Only the current stage packet reaches it (`references/adapter-protocol.md:31`).
- **Identity is observed, never echoed.** `proof.model` / `proof.effort` come from `ThreadStartResponse`, not from `requested_identity`. Echoing the request is not evidence (`references/adapter-protocol.md:16`).
- **Evidence or nothing.** Missing identity, session, usage or terminal-status evidence exits non-zero with empty stdout. A measured zero is never fabricated (`references/adapter-protocol.md:25`).
- **The registry is untouched.** Admission for the pilot is a private file outside the repo, passed via the existing `--registry / --registry-sha256` flags. There is no default-profile route and no `~/.hermes` profile mutation anywhere in this slice.
- **Pinning binds exactly four objects, and nothing transitively.** The directly bound objects are: the Python 3.13 ELF, `scripts/luna_codex_adapter.py`, the Codex native ELF, and the probe anchor file. The Python standard library, the dynamic loader, `/proc`, and the entire Codex npm distribution and package closure (`@openai/codex`, `codex.js`, `node_modules`, any file the ELF opens at runtime) are **trusted external prerequisites**, not pinned identities. This plan makes no recursive package-identity claim. See §2e.

## Tech Stack

- Python 3.13, standard library only. No new dependencies; none may be added.
- `scripts/luna_codex_adapter.py` is **self-contained**: it executes as `/proc/self/fd/<n>` under `python3.13 -I -S`, so sibling imports are impossible by construction. It must not import `stage_contracts`, `schema_validation`, `executor_*`, `harness_adapter`, `pinned_runtime` or `runtime_support`.
- JSON contracts validated by `scripts/schema_validation.py` — the documented strict subset (only `schema_validation.KEYWORDS`, `$ref` only as `#/$defs/<name>`).
- Canonical JSON: `json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(',',':'), allow_nan=False).encode('utf-8')`. The adapter re-implements this locally and a test asserts byte-identity with `schema_validation.canonical`.
- Tests: `unittest`, discovered from `scripts/tests`, extending the existing synthetic-fixture pattern (`routing_fixtures.ADAPTER` / `attach_adapter`, `routing_fixtures.py:62-97`).
- Codex protocol frozen from the installed CLI via `codex app-server generate-json-schema --out <dir>` (no model call).

## Global Constraints

**Non-goals — none of these may be built, enabled, or partially prepared in this slice:**

- No activation of any route in `assets/executor-routes.json`. **That file is not edited.** Every real executor route stays `status:"unverified"`, `adapter:null`, `model:null`.
- No `bounded_write` Luna execution, and therefore no write benchmark. `write_benchmark_sha256` stays `null` everywhere.
- No adapters for `luna_low`, `luna_medium`, `claude_sonnet_medium`, `claude_opus_high`.
- No retries, fallbacks, model aliasing, or automatic degradation to another model. One attempt, one model, or a clean refusal.
- No parsing, trusting, or persisting of the final assistant message. Worker output is declared workspace artifacts.
- No raising of `MAX_EXECUTABLE`, of the adapter `timeout_seconds` cap of 120, or of any stage limit.
- No claim of OS sandboxing, network isolation, cost/latency/quota characterisation, or recursive pinning of Codex's transitive dependencies. **Pinning covers exactly the four directly bound objects of §2e and nothing else.** No statement anywhere in this slice may imply that the Codex npm distribution, its package closure, the Python standard library, or the dynamic loader are identity-verified.
- No weakening of the observed-sandbox check to accommodate a field the real server may omit. If `ThreadStartResponse.sandbox.networkAccess` is not present and exactly `false`, the run refuses and the route stays unadmitted (§3.6, §10.1).
- No default-profile route, no edit under `~/.hermes/skills/`, no `~/.codex/config.toml` edit. The only registry the pilot uses is a private file created by `tools/pilot_registry.py` outside the repo.
- No live model call in the test suite, in `route-doctor`, or anywhere in the implementation session.
- These files are **not** modified: `scripts/executor_runtime.py`, `scripts/executor_routes.py`, `scripts/harness_adapter.py`, `scripts/bounded_loop.py`, `scripts/loop_menu.py`, `scripts/bounded_runtime.py` (except its version literal), `scripts/typed_jev.py`, `scripts/outbound_admission.py`, `scripts/stage_transition.py`, `scripts/semantic_cascade.py`, `scripts/context_rerank.py`, `scripts/runtime_support.py`, `scripts/schema_validation.py`, `assets/executor-routes.json`, `assets/schemas/stage-envelope.schema.json`, `assets/schemas/worker-packet.schema.json`, `assets/schemas/route-receipt.schema.json`, `assets/schemas/loop-*.schema.json`, `scripts/tests/routing_fixtures.py`, `scripts/tests/loop_fixtures.py`, `scripts/tests/test_bounded_loop.py`, `scripts/tests/test_executor_contracts.py`, `scripts/tests/test_executor_runtime.py`.
- In particular the executor protocol itself is frozen: `harness_adapter.py` gains no parameter, no helper and no refactor. The adapter conforms to the existing envelopes exactly as they are.
- `tools/` is deliberately **outside** the installed skill bundle. The bundle manifest must not gain entries for it.

**Binding invariants:**

- The probe makes **no model call** — `initialize` only, never `thread/start`, never `turn/start`.
- The adapter's child environment is constructed from a literal dict. `os.environ.copy()` must not appear in the file.
- The adapter must **not** pass `start_new_session=True` to its Codex child; staying inside the harness process group is what lets `os.killpg` at `harness_adapter.py:67` reap it.
- On any failure the adapter writes **nothing** to stdout and exits non-zero. stderr is diagnostic only and is discarded at the OS level by `stderr=subprocess.DEVNULL` (`harness_adapter.py:32`); the controller never persists it.
- `native_pins` is optional. Every pre-existing adapter spec must behave byte-identically. When present it must be honoured at **both** enforcement sites — `stage_contracts.validate_pins` (which drives `pinned_hash(..., executable=)`) and `pinned_runtime.bind_command` — because `validate_pins` runs first and would otherwise reject the Codex ELF before `bind_command` is ever entered (§2).
- The drain loop terminates only when **both** a matching terminal `turn/completed` and a matching `thread/tokenUsage/updated` have been observed, under one shared monotonic deadline. Either one alone is a refusal (§3.6).
- Final acceptance stays parent-only. Nothing this slice emits sets `parent_acceptance` true.

---

## 1. Ground truth established before writing this plan

Do not re-derive these.

### 1a. Repository (`008e78f`, branch `feat/luna-executor-adapter`)

| Fact | Source |
|---|---|
| `PROTOCOL='hermes-executor-v1'` | `scripts/harness_adapter.py:18` |
| Probe response key set exactly `{protocol,operation,ready,identity,supported_modes,evidence_kind}` | `scripts/harness_adapter.py:96` |
| Run response key set exactly `{protocol,operation,evidence_kind,proof,usage}` | `scripts/harness_adapter.py:117` |
| `PROOF_FIELDS = ['harness','model','effort','session_id','permissions','packet_hash','nonce','exit_code']` | `scripts/stage_contracts.py:10` |
| `usage` exactly `{input_tokens,output_tokens}`, ints in `[0, 1_000_000_000]` | `scripts/harness_adapter.py:129` |
| `permissions` must equal `{mode, allowed_paths, network:False}` | `scripts/harness_adapter.py:123` |
| Child env allowlist — 4 keys, **no `HOME`** | `scripts/harness_adapter.py:26` |
| `stderr=DEVNULL`, `start_new_session=True`, `killpg` on exit | `scripts/harness_adapter.py:30-32,64-72` |
| stdin budget 128 KiB (`adapter_input_budget`), stdout budget 128 KiB (`adapter_output_budget`) | `scripts/harness_adapter.py:25,17,55` |
| Probe cwd is `Path(spec['argv'][-1]).parent`, outside the worker tree | `scripts/harness_adapter.py:93` |
| FD aliasing: `argv[0]` → `Popen(executable=)`; every *other* pinned absolute argv item → `/proc/self/fd/<n>`; `{workspace}` → cwd | `scripts/pinned_runtime.py:172-175` |
| `is_executable = path == spec['argv'][0]` **only**; other pins capped at `MAX_DOCUMENT` 16 MiB and `fchmod 0o400` | `scripts/pinned_runtime.py:137-139,162` |
| **`validate_pins` hashes with `pinned_hash(p,executable=(path==args[0]))`, so a non-`argv[0]` pin is capped at `MAX_DOCUMENT` and a 246.7 MiB ELF raises `source_size` *before* `bind_command` runs** | `scripts/stage_contracts.py:68`, `scripts/pinned_runtime.py:78,113-114` |
| `validate_pins` runs **twice** per `run_command` (pre- and post-execution) and once more inside `validate_registry` | `scripts/harness_adapter.py:22,60`, `scripts/executor_routes.py:35` |
| `MAX_DOCUMENT=16 MiB`, `MAX_EXECUTABLE=256 MiB`, `MAX_COMMAND_BYTES=512 MiB` | `scripts/pinned_runtime.py:17-19` |
| Shebang refused: `\x7fELF` required for executables | `scripts/pinned_runtime.py:145-146` |
| `validate_pins`: no shell, no `-c`, no placeholder but `{workspace}`, `set(pins) == absolute argv items`, absolute `argv[0]`, no pin inside the worker tree | `scripts/stage_contracts.py:55-68` |
| `'luna'` is on the model-alias denylist; `approved-for-pilot` needs `model`+`effort`+`approval_sha256`; `write_benchmark_sha256` required only for `bounded_write` | `scripts/executor_routes.py:24-30` |
| `available()` needs `stage.mode == route.mode`, `stage.role ∈ route.roles`, risk ≤ `max_risk`, classification ≠ `private`, and (`synthetic` or `external_allowed`) | `scripts/executor_routes.py:73-83` |
| `_ready_ok` re-checks route/registry/stage digests, finite non-expired `expires_at` (TTL 300 s), identity, `stage.mode ∈ supported_modes`, `adapter_digest` | `scripts/executor_routes.py:54-61`, `harness_adapter.py:85` |
| `evidence_mode='live'` refuses `synthetic` classification and any synthetic adapter among candidates | `scripts/executor_runtime.py:78-81` |
| `_admit_gates` fires **only if some `stage.features` is true** | `scripts/executor_runtime.py:82-83,114-122` |
| `route_cli` accepts an arbitrary `--registry <path> --registry-sha256 <sha>` | `scripts/route_cli.py:64-65,103` |
| `read_only` ⇒ `allowed_paths == []` ⇒ `max_changed_files == 0` and `required_paths ⊆ input paths` (`required_paths` minItems 1) | `scripts/stage_contracts.py:90-94` |
| Adapter `timeout_seconds` schema max **120**; stage `max_seconds` max 900; `max_worker_calls` max 2 | `assets/schemas/executor-registry.schema.json:42-45`, `assets/schemas/stage-envelope.schema.json` |
| `data_policy.classification ∈ {synthetic, public-redacted, private}` | `assets/schemas/stage-envelope.schema.json` |
| Worker packet ≤ 64 KiB canonical | `scripts/stage_contracts.py:146` |
| Tests: `unittest` only, no pytest, no CI, no lint/type tooling anywhere in the repo | `README.md:29-33`; verified by exhaustive `find` |
| **Version literals are asserted in eight places, not seven**: `test_route_cli.test_candidate_version` pins `bounded_runtime.SKILL_VERSION`, `install_skill.VERSION` and the `SKILL.md` frontmatter as three independent `'1.3.0'` literals | `scripts/tests/test_route_cli.py:46-48` |
| `test_bundle_skill_version` additionally asserts `installer.VERSION == MANIFEST_VERSION` in `setUp` | `scripts/tests/test_bundle_skill_version.py:27,34-35` |
| Shipped `luna_max`: executor / harness `luna` / skill `luna-task-routing` / effort `max` / **mode `bounded_write`** / status `unverified` / adapter `null` / model `null` / roles `["implement","review"]` / max_risk `medium` | `assets/executor-routes.json` |

### 1b. Installed Codex (verified locally, no model calls)

- `codex` → `~/.local/bin/codex` → `codex.js`, a **Node shebang script** — not a legal `argv[0]` (`explicit_native_runtime_required`).
- Native ELF (musl-static):
  `/home/ivan/.local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex`
  **258 659 424 bytes**, mode `0700`.
- `codex --version` → `codex-cli 0.153.4`.
- `gpt-5.6-luna` present in `~/.codex/models_cache.json` (siblings: `gpt-5.5`, `gpt-5.6-sol`, `gpt-5.6-terra`).
- Protocol captured with `codex app-server generate-json-schema --out <dir>`:

| Item | Verified shape |
|---|---|
| ClientRequest methods used | `initialize`, `thread/start`, `turn/start`, `turn/interrupt` |
| **`ClientNotification`** | a `oneOf` with exactly **one** member: `initialized`. It is the only notification a client may send, and the adapter must send it (§3.6 step 2) |
| `InitializeParams` | `{clientInfo (required), capabilities?}`; `ClientInfo` required `{name, version}`, optional `title` |
| `InitializeResponse` | required `{codexHome, platformFamily, platformOs, userAgent}` |
| `ThreadStartParams` | no `required`; properties `model`, `cwd`, `sandbox`, `approvalPolicy`, `approvalsReviewer`, `threadSource`, `ephemeral`, `config`, `baseInstructions`, `developerInstructions`, `modelProvider`, `personality`, `serviceName`, `serviceTier`, `sessionStartSource` — **no `effort`** |
| `ThreadStartParams.sandbox` | `SandboxMode \| null` (the **string** form) |
| `ThreadStartParams.config` | `object \| null`, `additionalProperties: true` — arbitrary keys accepted by the schema, so a wrong key is *not* rejected at the protocol layer (§3.2) |
| `SandboxMode` | `read-only` \| `workspace-write` \| `danger-full-access` |
| `AskForApproval` | `untrusted` \| `on-request` \| `never` \| `{granular:{...}}` |
| `ApprovalsReviewer` | `user` \| `auto_review` \| `guardian_subagent` |
| `ThreadStartResponse` | required `{approvalPolicy, approvalsReviewer, cwd, model, modelProvider, sandbox, thread}`; optional `reasoningEffort`, `serviceTier`, `instructionSources`. **`sandbox` is a `SandboxPolicy` object**, not the `SandboxMode` string |
| `ThreadStartResponse.reasoningEffort` | `ReasoningEffort \| null`, **optional** |
| **`SandboxPolicy` / `ReadOnlySandboxPolicy`** | `{type:"readOnly", networkAccess?: boolean}`; `required` is **`["type"]` only** — `networkAccess` carries `"default": false` and **may legitimately be absent from the wire**. This is the field §3.6 refuses to weaken |
| `Thread` | required `{cliVersion, createdAt, cwd, ephemeral, id, modelProvider, preview, projectId, sessionId, source, status, turns, updatedAt}`. **`model`, `reasoningEffort` and `threadSource` are OPTIONAL and nullable** — see the risk row in §13 |
| `Thread.reasoningEffort` | schema description, verbatim: *"Current configured reasoning effort when loaded, otherwise the latest persisted effort. Null when unset or unavailable. This is not per-turn execution telemetry."* |
| `ReasoningEffort` | `{"type":"string","minLength":1}` — a free-form non-empty string, **not an enum**. `"max"` is therefore schema-legal and is *not* validated by the protocol |
| `ThreadSource` | `{"type":"string"}` — free-form. `"user"` is schema-legal and unvalidated |
| `TurnStartParams` | required `{threadId, input}`; optional `effort`, `model`, `cwd`, `sandboxPolicy`, `approvalPolicy`, `approvalsReviewer`, `outputSchema`, `personality`, `summary`, `turnTrigger`, `serviceTier`, `toolOutput`, `clientUserMessageId` |
| `TurnStartParams.turnTrigger` | `string \| null`, free-form and unvalidated |
| `UserInput` (text variant) | required `{type:"text", text}` |
| `TurnStartResponse` | required `{turn}`; `Turn` required `{id, items, status}`, optional `error`, `startedAt`, `completedAt`, `durationMs`, `itemsView` |
| `TurnStatus` | `completed` \| `interrupted` \| `failed` \| `inProgress` |
| Notifications | `thread/started`, `turn/started`, `turn/completed`, `thread/tokenUsage/updated`, `error`, plus many `item/*` |
| **`TurnStartedNotification`** | required `{threadId, turn}` — **there is no `params.turnId`**; the turn id is `params.turn.id` |
| **`TurnCompletedNotification`** | required `{threadId, turn}` — **there is no `params.turnId`**; the turn id is `params.turn.id` and the status is `params.turn.status` |
| `ThreadStartedNotification` | required `{thread}` — **carries no top-level `threadId`**; the thread id is `params.thread.id` |
| `ThreadTokenUsageUpdatedNotification` | required `{threadId, turnId, tokenUsage}` — **this is the only turn-scoped notification the adapter uses that has `params.turnId`** |
| `ThreadTokenUsage` | required `{last, total}`, optional `modelContextWindow`; `TokenUsageBreakdown` required `{inputTokens, outputTokens, cachedInputTokens, reasoningOutputTokens, totalTokens}`, optional `cacheWriteInputTokens` |
| ServerRequests (all must be declined) | `applyPatchApproval`, `execCommandApproval`, `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval`, `item/tool/call`, `item/tool/requestUserInput`, `mcpServer/elicitation/request`, `attestation/generate`, `account/chatgptAuthTokens/refresh` — exactly ten, matching the frozen `ServerRequest.json` |
| No-model-call quota read | `account/rateLimits/read`, `account/usage/read` |
| No-model-call config read | `configRequirements/read` (used by §10.1 `--dump-config-requirements`) |
| Codex ELF | `258 659 424` bytes, mode `0700`, `\x7fELF`; `258659424 / 268435456 = 96.36 %` of `MAX_EXECUTABLE` |
| `/usr/bin/python3.13` | `6 812 336` bytes (6.50 MiB), mode `0755`, root-owned ELF |
| `~/.codex` | contains `auth.json` (mode `0600`) and `config.toml`; the adapter supplies `CODEX_HOME` and never edits either file |

> **Correction 1 to the original brief, carried through this plan.** Reasoning effort is **not** a `thread/start` parameter in 0.153.4. It is a `turn/start` parameter and a thread-level `config` value. The adapter sets it in both places and *proves* it from `ThreadStartResponse.reasoningEffort`.

> **Correction 2 — the turn id is not where the original brief assumed.** `turn/started` and `turn/completed` carry `params.turn.id`; **only** `thread/tokenUsage/updated` carries `params.turnId`. Scoping every turn-related notification on `params.turnId` would mean the adapter never matches its own `turn/completed` and every real run fails `turn_not_completed`, while a fake built to the same wrong assumption passes. §3.6 and §8.1 both encode the real shapes.

> **Correction 3 — `initialized` is part of the handshake.** The frozen `ClientNotification` union has exactly one member. The adapter sends `{"jsonrpc":"2.0","method":"initialized"}` after a valid `InitializeResponse` and before any further request, on both the probe and the run path.

> **Correction 4 — `reasoningEffort` is a thread-level configured value.** The schema states in terms that it is *not per-turn execution telemetry*. `proof.effort` therefore attests the effort the thread was configured with, not a measurement that the turn executed at that effort. That sentence is repeated verbatim in `references/luna-adapter.md` and in the pilot `boundary.md`.

---

## 2. Blocker resolved first: pinning a 246.7 MiB native executable

Two *independent* core sites treat only `argv[0]` as an executable, and **both** must change. Changing `bind_command` alone is not sufficient and the earlier claim that it was is retracted.

**Site 1 — `stage_contracts.validate_pins:68` (runs first).**

```python
if pinned_hash(p,executable=(path==args[0]))!=expected:raise ContractError('command_pin_changed')
```

`pinned_hash` selects `limit = MAX_EXECUTABLE if executable else MAX_DOCUMENT` (`pinned_runtime.py:113-114`), and `regular_fd` raises `ContractError('executable_size' if executable else 'source_size')` at `pinned_runtime.py:78`. So the 258 659 424-byte Codex ELF raises **`source_size` inside `validate_pins`**, before `bind_command` is ever entered. This fires on every path that touches the spec: `harness_adapter.run_command:22`, `harness_adapter.run_command:60`, and `executor_routes.validate_registry:35`.

**Site 2 — `pinned_runtime.bind_command:137`.**

```python
is_executable = path == spec['argv'][0]
```

The Codex ELF passed as a later pinned argv item fails here too: capped at `MAX_DOCUMENT` → `ContractError('source_size')`, and the snapshot would be `fchmod 0o400`, so the adapter could never exec `/proc/self/fd/<n>`.

"The adapter receives the Codex executable as its FD alias argument" is unreachable without changing both.

**Fix: an explicit, optional `native_pins` declaration, honoured at both sites. Not content sniffing.**

### 2a. `assets/schemas/executor-registry.schema.json`

Add one optional property inside the adapter object. Leave `required` at the existing five keys, so every pre-existing spec stays valid.

```json
"native_pins": {
  "type": "array",
  "maxItems": 4,
  "uniqueItems": true,
  "items": {"type": "string", "minLength": 1, "maxLength": 2048}
}
```

### 2b. `scripts/stage_contracts.py::validate_pins` — shape checks **and** the hash-limit selector

The local parameter is named `command`, not `spec`. Two edits, both required.

*(i)* Append the shape checks, in the file's dense style, after the existing `argv` / `pins` checks and **before** the per-pin loop:

```python
native=command.get('native_pins') or []
if type(native) is not list or len(native)>4 or len(set(native))!=len(native):raise ContractError('native_pin_shape')
if not set(native)<=set(pins) or args[0] in native:raise ContractError('native_pin_not_pinned')
native=set(native)
```

**Two reason codes, and the matrix is closed — there is deliberately no `native_pin_not_in_argv`.** The pre-existing check at `stage_contracts.py:59-61` already enforces `set(pins) == {a for a in args if Path(a).is_absolute()}`. Therefore `native ⊆ pins` implies every native entry *is* an absolute `argv` item, and `args[0] in native` is rejected on the same line, which leaves exactly `args[1:]`. A membership test would be unreachable: any spec that could trigger it raises `incomplete_command_pins` or `native_pin_not_pinned` strictly earlier. Test A13 asserts that closure rather than asserting a dead code path.

*(ii)* **Change the existing hash call inside the per-pin loop** so the executable limit and the `+x`/no-setuid check apply to native pins as well:

```python
# was: if pinned_hash(p,executable=(path==args[0]))!=expected:
if pinned_hash(p,executable=(path==args[0] or path in native))!=expected:raise ContractError('command_pin_changed')
```

Without *(ii)* the Codex pin raises `source_size` at `stage_contracts.py:68` and the slice cannot go green — size test A9 would observe `source_size`, not `executable_size`; setuid/execute test A8 would not exercise the native-executable permission branch at all.

`argv[0]` is native by definition; `native_pins` names *additional* native executables only.

### 2c. `scripts/pinned_runtime.py::bind_command`

Two lines **at this site**, on top of §2b. Only once both sites agree do the remaining guarantees — the 256 MiB limit, the `\x7fELF` check, the `+x`/no-setuid check, `fchmod 0o500`, `_seal_snapshot`, the sealed re-hash — apply to the Codex ELF.

```python
native=set(spec.get('native_pins') or ())
...
is_executable = path == spec['argv'][0] or path in native
```

Here the parameter *is* named `spec`.

### 2d. Consequences to document in `references/adapter-protocol.md`

- Widens "may be a 256 MiB sealed executable memfd" from 1 to ≤5 objects per command. Each remains SHA-256-pinned, `O_NOFOLLOW`-opened per path component, ELF-checked, sealed (`SEAL|SHRINK|GROW|WRITE`), re-hashed after sealing, and mode `0500`. This grants no new privilege — the adapter could already `exec` arbitrary bytes; pinning makes Codex *more* constrained than before.
- Cost, stated accurately: ~246.7 MiB copied into a memfd plus **four** full SHA-256 passes per `run_command` — `validate_pins` at `harness_adapter.py:22`, `bind_command`'s copy-hash, `bind_command`'s sealed re-hash, and `validate_pins` again at `harness_adapter.py:60`. An attempt performs one probe `run_command` and one run `run_command`, so **eight** passes and two ~247 MiB memfd copies per attempt. Budget roughly 1.5–3 s and ~250 MiB tmpfs residency per invocation on this machine. Not optimised in this slice.
- The first `validate_pins` (`:22`) runs *before* `started=time.monotonic()` (`:27`); the second (`:60`) and all of `bind_command` run inside the `timeout_seconds` window. With the cap at 120 s this is not a constraint, but it is why the pilot prompt is deliberately trivial.
- Headroom: 258 659 424 / 268 435 456 = **96.36 %** of `MAX_EXECUTABLE`. The next Codex release will break this. Raising the cap is a separate reviewed change; this slice only guarantees the failure is the legible `executable_size` at both enforcement sites.

### 2e. What pinning actually binds — and what it does not

Stated once here, repeated verbatim in `references/adapter-protocol.md`, `references/luna-adapter.md` and the pilot `boundary.md`.

**Directly bound (SHA-256 pinned, `O_NOFOLLOW`-opened, sealed memfd, re-hashed after sealing):**

1. `/usr/bin/python3.13` — the native ELF interpreter, `argv[0]`, mode `0500` snapshot;
2. `scripts/luna_codex_adapter.py` — the adapter source, mode `0400` snapshot;
3. the Codex native ELF — via `native_pins`, mode `0500` snapshot;
4. the probe anchor file (§3.1) — mode `0400` snapshot.

**Trusted external prerequisites — NOT identity-verified by anything in this slice:**

- the Python standard library, `site` machinery excluded by `-I -S`, the dynamic loader, libc, and `/proc`;
- the **entire Codex npm distribution and package closure**: `@openai/codex`, `codex.js`, `node_modules/`, the `codex-linux-x64` vendor tree, and every file the Codex ELF opens, maps, spawns or downloads at runtime;
- `~/.codex/` — `auth.json`, `config.toml`, caches, plugins, agents;
- the OS kernel, its sandboxing behaviour, and the network path to any provider.

Pinning object 3 proves *which bytes are executed as the Codex entry point*. It proves nothing about what those bytes then read or execute. **No recursive package-identity claim is made or may be made.** A statement such as "Codex is pinned" is only true of the entry-point ELF and must always be written that way.

---

## 3. Contract: `scripts/luna_codex_adapter.py`

Single file, stdlib only, no repo imports.

### 3.1 Invocation contract

```
argv        = [<abs python3.13 ELF>, '-I', '-S', <abs luna_codex_adapter.py>,
               <abs codex ELF>, *prefix, '--probe-anchor', <abs probe anchor file>]
pins        = {python3.13: sha256, luna_codex_adapter.py: sha256, codex ELF: sha256,
               probe anchor: sha256, *absolute prefix items: sha256}
native_pins = [<abs codex ELF>]
```

Inside the adapter:

- `sys.argv[0]` — its own fd alias (`/proc/self/fd/B`);
- `sys.argv[1]` — the **Codex executable fd alias** (`/proc/self/fd/C`);
- `sys.argv[2:]` — the launch prefix, **followed by a mandatory trailing `--probe-anchor <path>` pair**. The prefix proper is `sys.argv[2:-2]`: empty in production, and in the offline tests it carries `-I -S <fake server fd alias> <scenario token>`.

**The probe anchor (`--probe-anchor`) — why the last argv item is what it is.**

`harness_adapter.readiness` derives the probe cwd as `Path(spec['argv'][-1]).parent` (`harness_adapter.py:93`), from the **unbound** spec argv, not from the fd-aliased args. That derivation is *not* changed by this slice — `harness_adapter.py` is in the do-not-modify list. Instead the spec is written so the derivation lands somewhere intended:

- the final argv item is an **absolute, pinned, harmless anchor file** (a small JSON stub, `{"purpose":"hermes-luna-probe-anchor","schema_version":1}`) that lives alone in a **disposable probe directory** outside the repo, outside the workspace and outside any Codex package tree — `~/.hermes/pilot/probe-anchor/` in production, the test's `tmp/probe-anchor/` in the fixtures;
- the probe therefore runs with `cwd = <disposable probe dir>`, deterministically and identically in test and production;
- the scenario token, when present, is passed **before** the `--probe-anchor` pair, so it is never the final argv item.

Without the anchor the production spec would end in the Codex ELF and the probe would run with `cwd` inside `node_modules/@openai/codex/.../vendor/.../bin`, while the fixture would end in a bare scenario token and `Path('good').parent == Path('.')` would silently make the probe inherit the test runner's cwd. Test C9 would then pass by accident. The anchor makes it causal.

**Adapter-side parsing — validate, then ignore.** The adapter requires the last two elements to be exactly `'--probe-anchor'` and a path. It then *validates* that path before discarding it: it must be `/proc/self/fd/<n>` or absolute, must `os.stat` as a regular file, must be readable, and its bytes must parse as the anchor stub. Only after that validation succeeds is the pair stripped from the prefix and the anchor fd excluded from the child's `pass_fds`. A missing, malformed, unreadable or non-stub anchor is `probe_anchor_invalid` — never a silently tolerated stray argument. The anchor is **never** forwarded to Codex.

`_fd_aliases(argv)` collects every element matching `^/proc/self/fd/(\d+)$` and passes those fds via `pass_fds=` on the adapter's own `Popen`. **Without this, CPython closes the inherited fds immediately before `execv` and `/proc/self/fd/C` no longer resolves.** This is the single most failure-prone line in the file and has a dedicated test.

Spawn shape:

```python
subprocess.Popen(['codex', *prefix, *subcommand],           # prefix excludes the anchor pair
                 executable=codex_alias, pass_fds=fds,      # fds exclude the anchor fd
                 cwd=<probe: inherited disposable probe dir | run: workspace>,
                 stdin=PIPE, stdout=PIPE, stderr=DEVNULL,
                 env=child_env)          # NO start_new_session
```

### 3.2 Module constants — the reviewable surface

```python
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
PROBE_ANCHOR_STUB={'purpose':'hermes-luna-probe-anchor','schema_version':1}
PROBE_ANCHOR_MAX_BYTES=4096
```

`EFFORT_CONFIG_KEY` is the one value that could not be proven offline, and `ThreadStartParams.config` is `additionalProperties: true`, so the protocol layer will **not** reject a wrong key. It is therefore **not trusted**: the adapter verifies `ThreadStartResponse.reasoningEffort == 'max'`, so a wrong key degrades to a clean refusal (`effort_not_proven`) rather than a silent default-effort run. Confirming it is a named step in §10.1.

`CLIENT_INFO` matches the frozen `ClientInfo` shape (required `name`, `version`; optional `title`).

### 3.3 Environment — derived, never inherited

```python
home=pwd.getpwuid(os.getuid()).pw_dir              # NOT os.environ['HOME']
if not (home and os.path.isabs(home) and os.path.isdir(home)):fail('home_unavailable')
codex_home=os.path.join(home,'.codex')
tmpdir=tempfile.mkdtemp(prefix='hermes-luna-')     # removed in finally
child_env={'PATH':os.defpath,'HOME':home,'CODEX_HOME':codex_home,
           'LANG':'C.UTF-8','LC_ALL':'C.UTF-8','TERM':'dumb','TMPDIR':tmpdir}
```

Literal dict. `os.environ.copy()` must not appear anywhere in the file. No Hermes, gateway, GitHub, cloud, proxy or provider variable is ever forwarded — the adapter's own environment is already the 4-key controller allowlist, and the child env is built from scratch regardless.

### 3.4 Public symbols

| Symbol | Contract |
|---|---|
| `canonical(value) -> bytes` | byte-identical to `schema_validation.canonical` |
| `digest(value) -> str` | `sha256(canonical(value)).hexdigest()` |
| `_fd_aliases(argv) -> tuple[int,...]` | fds referenced by `/proc/self/fd/<n>` argv items |
| `_split_argv(argv) -> (codex_alias, prefix, anchor)` | enforces the §3.1 layout: `argv[1]` is the Codex alias, the last two items are exactly `PROBE_ANCHOR_FLAG` and the anchor path, `prefix = argv[2:-2]`. Raises `argv_shape` if the layout is wrong |
| `_validate_anchor(path) -> None` | stat-regular, readable, ≤ `PROBE_ANCHOR_MAX_BYTES`, parses to `PROBE_ANCHOR_STUB` exactly; else `probe_anchor_invalid`. Called before the anchor is dropped |
| `_child_env() -> dict` | §3.3 |
| `_prompt(packet) -> str` | §3.7 |
| `_Rpc(proc, deadline)` | newline-delimited JSON-RPC client, §3.5; exposes `request()`, `notify()`, `next_message()` |
| `_handshake(rpc, codex_home) -> dict` | `initialize` → verify `InitializeResponse` → send the `initialized` notification. Shared by probe and run (§3.6 steps 1–2) |
| `probe(codex_alias, prefix) -> dict` | §3.8 |
| `run(request) -> dict` | §3.6 |
| `main(argv=None, stdin=None) -> int` | reads one JSON object from stdin, dispatches on `operation`, prints exactly one JSON object on success, returns 0/1 |

### 3.5 JSON-RPC client bounds

Newline-delimited JSON over the child's stdin/stdout, `selectors`-driven so a chatty server cannot deadlock the writer. Bounds: `MAX_LINE` 1 MiB per frame, `MAX_TOTAL_RX` 32 MiB per session, `MAX_NOTIFICATIONS` 4096, one monotonic deadline shared across the whole session — including the drain of §3.6 step 6 — and re-checked before every read. Frames are parsed with `object_pairs_hook` rejecting duplicate keys, `parse_constant` rejecting non-finite numbers, and anything that is not a dict rejected outright. Child stderr → `DEVNULL`.

`notify()` writes a JSON-RPC notification (no `id`, no response awaited) and is used for exactly one method, `initialized`.

**These bounds are the adapter's *receive* budget and are distinct from the controller's stdout budget.** Exceeding `MAX_LINE` / `MAX_TOTAL_RX` / `MAX_NOTIFICATIONS` is an adapter-side refusal: empty stdout, non-zero exit, reason `rx_budget`, which the controller sees as `worker_exit_failure`. The controller's 128 KiB `adapter_output_budget` (`harness_adapter.py:17,55`) can only fire if the adapter itself writes more than 128 KiB to *its own* stdout, which §3.10 forbids by construction. The two are separately observed: **B22** (oversized JSON-RPC line) and **B23** (notification flood) exercise the adapter's `rx_budget` and surface as `worker_exit_failure`; **B24** (`huge_output`) exercises the controller's `adapter_output_budget` and surfaces as that `ContractError`. Distinguishing the two observations is the point of having three tests.

### 3.6 Run sequence and verification table

1. `initialize` with `{clientInfo: CLIENT_INFO}` → require `{codexHome, platformFamily, platformOs, userAgent}`, `platformOs == 'linux'`, `codexHome == codex_home`.
2. **`initialized` notification** — `{"jsonrpc":"2.0","method":"initialized"}`, no `id`, no response awaited. This is the only member of the frozen `ClientNotification` union and completes the handshake; it is sent before any further request on **both** the probe and the run path. Steps 1–2 are `_handshake()`.
3. `thread/start` with
   `{model: MODEL, cwd: <workspace>, sandbox: 'read-only', approvalPolicy: 'never', approvalsReviewer: 'user', threadSource: 'user', ephemeral: true, config: {EFFORT_CONFIG_KEY: EFFORT}}`.
4. Verify the `ThreadStartResponse`. **Every row fails closed.**

| Field | Required value | Failure code |
|---|---|---|
| `model` | `gpt-5.6-luna` | `model_mismatch` |
| `reasoningEffort` | `max`, present and non-null | `effort_not_proven` |
| `cwd` | `os.path.realpath(workspace)` | `cwd_mismatch` |
| `sandbox.type` | `'readOnly'` | `sandbox_mismatch` |
| `sandbox.networkAccess` | **present and `is False`** — the JSON literal `false`, checked with `is False` so `0`, `''`, `None` and a missing key all fail | `sandbox_network_not_proven` |
| `approvalPolicy` | `'never'` | `approval_policy_mismatch` |
| `approvalsReviewer` | `'user'` | `reviewer_mismatch` |
| `thread.threadSource` | present and `'user'` | `thread_source_mismatch` |
| `thread.id`, `thread.sessionId` | non-empty strings | `session_binding_missing` |
| `thread.model`, `thread.reasoningEffort` | present and equal to the verified values above | `thread_identity_mismatch` |

> **`sandbox.networkAccess` is deliberately strict, and deliberately not weakened.** `ReadOnlySandboxPolicy` requires only `type`; `networkAccess` carries `"default": false` and **may be absent on the wire**. The adapter does not accept absence as `false`, because `proof.permissions.network` is asserted to the controller as `False` and an absent field is not an observation. A default is not evidence. If the real 0.153.4 server omits the field, the correct outcome is that **`luna_max_read_only` is never admitted** — §10.1 gates the pilot on observing it, and §13 carries it as a named risk. There is no fallback, no inference from `sandbox.type`, and no config flag that relaxes this.

> **Three `Thread` fields are optional in the schema and required here.** `Thread.model`, `Thread.reasoningEffort` and `Thread.threadSource` are all optional and nullable in 0.153.4. Requiring them is a deliberate fail-closed choice: a thread that will not restate its own identity yields no proof. The consequence is that a real server which omits any of them refuses the run rather than degrading it — a **live-pilot risk**, carried in §13, not a synthetic-test concern.

5. `turn/start` with `{threadId, input:[{type:'text', text:_prompt(packet)}], effort:'max', model:MODEL, cwd:<workspace>, sandboxPolicy:SANDBOX_POLICY, approvalPolicy:'never', turnTrigger:TURN_TRIGGER}`. Capture `turn.id` from `TurnStartResponse.turn.id`.
6. **Drain until BOTH pieces of evidence are in hand**, under the one shared monotonic deadline: a matching terminal `turn/completed` **and** a matching `thread/tokenUsage/updated`. Neither alone ends the loop, and their arrival order is not assumed in either direction. The loop exits on: both observed → success; deadline, `MAX_NOTIFICATIONS`, `MAX_TOTAL_RX`, EOF, a scoped `error`, or any `ServerRequest` → refusal. A `turn/completed` with a non-`completed` status is terminal-and-refusing immediately, without waiting for usage.
7. `turn/interrupt` on every abort path; then close stdin, `terminate()`, bounded `wait`, `kill()`, `rmtree(tmpdir)` — all in `finally`.

**Notification scoping — the shapes differ, and this is where a plausible-looking adapter silently never completes**

| Notification | Thread id at | Turn id at | Ours when |
|---|---|---|---|
| `thread/started` | `params.thread.id` | — | `params.thread.id == thread_id` |
| `turn/started` | `params.threadId` | **`params.turn.id`** | both match |
| `turn/completed` | `params.threadId` | **`params.turn.id`** | both match |
| `thread/tokenUsage/updated` | `params.threadId` | **`params.turnId`** | both match |
| `error` | `params.threadId` when present | — | thread matches, or the notification is unscoped |

`TurnStartedNotification` and `TurnCompletedNotification` have required `{threadId, turn}` and **no `params.turnId` at all**; `ThreadTokenUsageUpdatedNotification` has required `{threadId, turnId, tokenUsage}` and **no `params.turn`**. Scoping everything on `params.turnId` would make `turn/completed` unmatchable and every real run fail `turn_not_completed`. The adapter reads each id from the field that notification actually carries, and the §8.1 fake emits these exact shapes — not a convenient normalised shape.

**Notification and server-request policy**

- *Scoping.* Foreign thread, foreign turn, an id read from the wrong field, missing ids or an unknown method → counted in `dropped_notifications`, otherwise ignored. It never advances state and never supplies evidence.
- *Errors.* An `error` notification scoped to our thread → `server_error_notification`.
- *Server requests.* **Any** inbound `ServerRequest` → reply `{"jsonrpc":"2.0","id":<id>,"error":{"code":-32001,"message":"approval_declined"}}`, record the method, send `turn/interrupt`, fail closed `server_request_declined`. A read-only single-turn bounded run has no legitimate reason to be asked for anything; being asked means the packet drove the model toward a side effect.
- *Terminal status.* Require `turn/completed` for our `(threadId, turn.id)` with `turn.status == 'completed'`. `interrupted` / `failed` / absent / deadline → `turn_not_completed`. `TurnStartResponse.turn.status` (typically `inProgress`) is never trusted.
- *Usage.* From the **last** `thread/tokenUsage/updated` matching our `(params.threadId, params.turnId)`, take `tokenUsage.total`: `input_tokens = total.inputTokens`, `output_tokens = total.outputTokens`, each required present, coerced to `int` and range-checked `0..1_000_000_000`. **No matching notification ⇒ `usage_evidence_missing`**, including when `turn/completed` arrived first and the drain then hit the deadline. Zeros are never fabricated.
- *Assistant text.* Every `item/*` delta and the final assistant message are discarded without parsing.

### 3.7 Prompt derivation — packet-only

`_prompt(packet)` is deterministic and derives text from exactly these fields:

`objective`, `mode`, `allowed_paths`, `forbidden`, `domain_rules[*].{skill,rule_id,text}`, `output_contract.required_paths`, `inputs[*].{id,path,sha256}`, `stop_conditions`, `selected_context[*].{id,source_sha256,text}`, `execution.attempt`, `execution.correction_evidence`.

Plus a fixed preamble: read-only, no shell escalation, no approvals, modify nothing, exactly one turn.

**Never in the prompt:** `nonce`, `packet_hash`, `route_receipt_hash`, `stage_hash`, `methodology_hash`, route ids, `workflow.*` source paths, or anything from the adapter's own environment.

Assert `len(prompt.encode('utf-8')) <= MAX_PROMPT_BYTES`, else `prompt_budget`.

### 3.8 Probe — no model call

1. Run `<codex-alias> *prefix --version`, strict env, ≤ `VERSION_TIMEOUT`. Parse `codex-cli <semver>`; require `>= MIN_CLI_VERSION`, else `cli_version_unsupported`; unparseable → `cli_version_unreadable`.
2. Start `<codex-alias> *prefix app-server`, send `initialize`, verify the `InitializeResponse` shape and `codexHome`, shut down. **No `thread/start`, no `turn/start`, no model call.**
3. Emit exactly:

```json
{"protocol":"hermes-executor-v1","operation":"probe","ready":true,
 "identity":{"harness":"luna","model":"gpt-5.6-luna","effort":"max"},
 "supported_modes":["read_only"],"evidence_kind":"live"}
```

Probe identity is the adapter's own pinned constants plus a proven CLI capability check — the honest bound. The *run* is what proves the model actually served the turn.

### 3.9 Proof assembly

```python
session_id='codex:'+thread_id+':'+session_uuid+':'+turn_id     # 116 chars for UUIDv7; assert 1<=len<=128
proof={'harness':HARNESS,'model':observed_model,'effort':observed_effort,'session_id':session_id,
       'permissions':{'mode':packet['mode'],'allowed_paths':packet['allowed_paths'],'network':False},
       'packet_hash':digest(packet),'nonce':packet['nonce'],'exit_code':0}
body={'protocol':PROTOCOL,'operation':'run','evidence_kind':EVIDENCE_KIND,'proof':proof,'usage':usage}
```

`observed_model` / `observed_effort` come from the verified `ThreadStartResponse`, **never** from `requested_identity`.

### 3.10 Output discipline

- Success: exactly one JSON object on stdout via `json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',',':'), allow_nan=False)`, nothing else, exit 0.
- Failure: **nothing on stdout**, one reason-code line on stderr, exit 1. The harness maps a non-zero exit to `worker_exit_failure` and discards stderr at the OS level, so no diagnostic is ever persisted by the controller.
- No partial or optimistic envelope is ever written. Missing identity, usage, session or terminal evidence all take this path.

---

## 4. Registry: the shipped file is not touched

`assets/executor-routes.json` is **not modified**. Admission for the pilot goes through `tools/pilot_registry.py` (new, **not installed**), mirroring how `scripts/tests/routing_fixtures.py:33-40` appends `fixture_worker`:

- reads `assets/executor-routes.json` and **appends** one route, leaving all nine shipped routes byte-identical;
- `--out` must be outside the repo/worktree (refuse otherwise); writes mode `0600`;
- `--probe-anchor <abs path>` is **required** and must be absolute, outside the repo/worktree, outside the pilot workspace and outside any Codex package tree — in practice a file under the private pilot root, `~/.hermes/pilot/probe-anchor/anchor.json` (§3.1). The tool **creates** it with exactly `canonical(PROBE_ANCHOR_STUB)` bytes at mode `0400` when absent, and otherwise **verifies** it is a regular file holding exactly those bytes; anything else refuses without writing;
- computes the four pin digests (python, adapter, Codex ELF, probe anchor), builds the adapter spec, computes `adapter_digest = digest(spec)`, then runs the §4.1 field-by-field approval validation **before** any write;
- `--bindings-only` prints the computed digests and writes nothing, so the human fills the approval record from real values instead of guessing them;
- prints `{path, sha256, route_id, adapter_digest, approval_sha256, probe_anchor_path, probe_anchor_sha256}`.

Pilot route:

```json
{"route_id":"luna_max_read_only","kind":"executor","harness":"luna","skill":"luna-task-routing",
 "model":"gpt-5.6-luna","effort":"max","mode":"read_only","roles":["review"],
 "max_risk":"low","max_complexity":"medium","status":"approved-for-pilot","fallback":"owner",
 "approval_sha256":"<sha256(canonical(approval record))>","write_benchmark_sha256":null,
 "required_runtime_proof":["harness","model","effort","session_id","permissions","packet_hash","nonce","exit_code"],
 "adapter":{"protocol":"hermes-executor-v1",
            "argv":["/usr/bin/python3.13","-I","-S","<abs luna_codex_adapter.py>","<abs codex ELF>",
                    "--probe-anchor","<abs probe anchor file>"],
            "pins":{"/usr/bin/python3.13":"<sha>","<abs luna_codex_adapter.py>":"<sha>",
                    "<abs codex ELF>":"<sha>","<abs probe anchor file>":"<sha>"},
            "native_pins":["<abs codex ELF>"],
            "timeout_seconds":120,"evidence_kind":"live"}}
```

- **The final `argv` item is the probe anchor.** `harness_adapter.readiness` derives the probe cwd as `Path(spec['argv'][-1]).parent` (`harness_adapter.py:93`), so it resolves to the disposable probe directory and never to the Codex vendor `bin` tree (§3.1). `--probe-anchor` is a relative token and therefore not a pin; the anchor path is absolute and is the fourth pin, matching §2e object 4 and §3.1's `pins` set.
- `"gpt-5.6-luna"` clears the alias denylist at `executor_routes.py:27` (not in the denied set, no `latest` substring).
- `mode:"read_only"` means `write_benchmark_sha256` may stay `null` (`:29`).
- `approval_sha256` is the real digest of the human-written approval record at `~/.hermes/pilot/luna-max-approval.json` — **never** a placeholder such as `'1'*64`.

### 4.1 Approval record — validated field-by-field before any registry write

The record is human-written. `tools/pilot_registry.py` **recomputes** every derivable value and compares it field by field; on any mismatch it prints the mismatching field names and exits non-zero **without writing the registry**. Nothing is inferred, defaulted or coerced, and no field is accepted on trust.

```json
{"schema_version":1,
 "route_id":"luna_max_read_only","purpose":"one bounded read-only pilot turn, no artifact production",
 "harness":"luna","model":"gpt-5.6-luna","effort":"max","mode":"read_only",
 "evidence_kind":"live","scope":"single-run","data_classification":"public-redacted",
 "cli_version":"codex-cli 0.153.4",
 "python_sha256":"<sha>","adapter_sha256":"<sha>","codex_sha256":"<sha>",
 "probe_anchor_path":"<abs probe anchor file>","probe_anchor_sha256":"<sha>",
 "adapter_digest":"<digest(spec)>",
 "approved_by":"parent","approved_at":"<RFC 3339 UTC>","expires_at":"<RFC 3339 UTC>"}
```

| Field | Validated against |
|---|---|
| `schema_version` | exactly `1` |
| `route_id` | the generated route's `route_id` — exactly `luna_max_read_only` |
| `purpose` | non-empty string; copied verbatim into the evidence bundle |
| `harness`, `model`, `effort`, `mode` | the generated route's `harness` / `model` / `effort` / `mode`, string equality each |
| `evidence_kind` | the generated adapter spec's `evidence_kind` — exactly `live` |
| `scope` | exactly `single-run` |
| `data_classification` | the pilot stage's `data_policy.classification` (§10.4) — exactly `public-redacted` |
| `cli_version` | the `codex --version` line captured in §10.1, string equality |
| `python_sha256`, `adapter_sha256`, `codex_sha256` | the recomputed pin digests of `argv[0]`, the adapter source and the Codex ELF |
| `probe_anchor_path` | the `--probe-anchor` argument **and** the final `argv` item, string equality with both |
| `probe_anchor_sha256` | the recomputed digest of the verified-or-created anchor stub |
| `adapter_digest` | `digest(spec)` of the spec the tool is about to write |
| `approved_by` | exactly `parent` |
| `approved_at` | parseable RFC 3339 UTC, not in the future |
| `expires_at` | parseable RFC 3339 UTC, strictly after `approved_at` and strictly in the future at write time |

Unknown keys, missing keys, `null` values and type mismatches all refuse. `approval_sha256` is `sha256(canonical(record))` over the validated record, and only that digest reaches the route.

---

## 5. Files

**New (shipped in the skill bundle)**

- `scripts/luna_codex_adapter.py`
- `references/luna-adapter.md` — must be linked from `SKILL.md`; `scripts/tests/test_skill_contract.py:25-31` resolves every relative link.

**New (not shipped)**

- `tools/pilot_registry.py`
- `tools/pilot_preflight.py`
- `docs/superpowers/plans/2026-09-19-luna-codex-adapter.md` (this file)

**New (tests)**

- `scripts/tests/luna_fixtures.py` — named `*_fixtures.py` so `-p test_*.py` discovery skips it
- `scripts/tests/test_native_pins.py`
- `scripts/tests/test_luna_adapter_protocol.py`
- `scripts/tests/test_luna_adapter_probe.py`

**Created outside the repo, by tooling, never committed**

- `~/.hermes/pilot/probe-anchor/anchor.json` — the probe anchor stub, created/verified by `tools/pilot_registry.py` (§4). The test fixtures create their own under `tmp/probe-anchor/` (§8.1). No anchor file is added to the repo or to the skill bundle.

**Modified**

- `scripts/pinned_runtime.py`, `scripts/stage_contracts.py`, `assets/schemas/executor-registry.schema.json` (§2)
- `references/adapter-protocol.md` (native-pins + Codex sections)
- `references/verification.md` (new boundary block at the top)
- `SKILL.md`, `README.md` — prose **and** version literal
- the **eight** version-literal sites of §9, which add `scripts/install_skill.py`, `scripts/bounded_runtime.py`, `scripts/route_cli.py`, `scripts/tests/test_bundle_skill_version.py`, `scripts/tests/test_skill_contract.py` and `scripts/tests/test_route_cli.py` to the two files above. `scripts/tests/test_route_cli.py` is **not** in the do-not-modify list of **Global Constraints**; its three literals at `:46-48` are the site the earlier seven-item count dropped.

---

## 6. Test conventions

Unchanged from the repo:

- `unittest`; discovery `-p test_*.py` from `scripts/tests`.
- Path bootstrap on line 8-ish: `SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))`.
- RED-first idiom: `try:\n    import luna_codex_adapter as L\nexcept ImportError:L=None`, then `self.assertIsNotNone(L,'luna codex adapter missing')` in `setUp`.
- `tempfile.TemporaryDirectory()` + `self.addCleanup(self.t.cleanup)`.
- Every file ends `if __name__=='__main__':unittest.main()`.
- Negative cases listed and written first.
- Stdlib only. Zero skips.
- No bare `sleep` for synchronisation — bounded polling loops.

---

## 7. TDD Slice A — native pins

**RED:** `scripts/tests/test_native_pins.py`
**GREEN:** §2a + §2b + §2c

```bash
python3 -B -m unittest discover -s scripts/tests -p test_native_pins.py -v
```

**14 tests, A1–A14.** The validation matrix is exactly two new reason codes — `native_pin_shape` and `native_pin_not_pinned` — plus the pre-existing pin reasons they hand off to.

| # | Test | Asserts |
|---|---|---|
| A1 | `test_native_pin_not_in_pins_refused` | `native_pin_not_pinned` |
| A2 | `test_native_pin_equal_argv0_refused` | `native_pin_not_pinned` |
| A3 | `test_native_pin_duplicate_refused` | `native_pin_shape` |
| A4 | `test_native_pin_over_four_refused` | `native_pin_shape` |
| A5 | `test_native_pin_non_list_refused` | `native_pin_shape` for a string and for a non-empty dict. Absence, `None` and any falsy value collapse to `[]` through `command.get('native_pins') or []` and are the **optional** case, not a shape error — the schema of §2a is what rejects a falsy non-array at the registry layer, and A14 covers that |
| A6 | `test_native_pin_non_elf_refused` | `explicit_native_runtime_required` |
| A7 | `test_native_pin_without_exec_bit_refused` | `executable_permissions` |
| A8 | `test_native_pin_setuid_refused` | `executable_permissions` |
| A9 | `test_native_pin_over_256mib_refused` | `executable_size` (sparse file via `os.truncate`, no 256 MiB write) |
| A10 | `test_native_pin_hash_drift_refused` | `command_pin_changed` |
| A11 | `test_native_pin_sealed_and_executable` | seal mask `15`, `os.pwrite` → `EPERM`, mode `0o500`, exec of `/proc/self/fd/<n>` succeeds |
| A12 | `test_non_native_pin_still_document_limited` | a 17 MiB non-native pin still yields `source_size` |
| A13 | `test_native_pin_absent_from_argv_is_already_refused` | proves the matrix is **closed**: a `native_pins` entry that is not an `argv` item cannot reach the native checks — `set(pins) == {absolute argv items}` (`stage_contracts.py:59-61`) raises `incomplete_command_pins` first, and dropping it from `pins` instead raises `native_pin_not_pinned`. This is why there is no `native_pin_not_in_argv` reason code |
| A14 | `test_registry_schema_accepts_and_rejects_native_pins` | `contract(..., 'executor-registry.schema.json')` accepts a valid array, rejects a non-array and a 5-element array; `required` is still the existing five keys |

Full suite must be green before slice B starts.

---

## 8. TDD Slice B and C — the adapter

### 8.1 `scripts/tests/luna_fixtures.py`

`FAKE_APP_SERVER` — a Python source string written to a temp file and launched as a **real subprocess speaking real newline-delimited JSON-RPC**. It fakes the *server*, never the adapter's own logic.

- `--version` → prints `codex-cli 0.153.4`, exit 0.
- `app-server` → handles `initialize`, the `initialized` **notification**, `thread/start`, `turn/start`, `turn/interrupt`; emits `thread/started`, `turn/started`, `thread/tokenUsage/updated`, `turn/completed`.
- **Handshake is enforced causally.** The fake records `initialized` in its method list and keeps an `initialized_seen` flag. Any request other than `initialize` that arrives while the flag is false is answered with JSON-RPC error `-32002 not_initialized` and is **not** processed — so `thread/start` is impossible before the notification. If the adapter ever stopped sending `initialized`, the happy path (B30) would fail rather than silently pass.
- Records, into a JSON sidecar file the test reads afterwards: every method received **in arrival order** (requests and notifications alike), the `initialized_seen` flag as observed at `thread/start`, its `cwd`, its `/proc/self/exe` target, and its full `os.environ`.
- Scenario selected by an argv token (the controller env is a hard allowlist, so an env var cannot be used). The token is passed **before** the mandatory `--probe-anchor <path>` pair and is therefore never the final argv item (§3.1).

Scenarios — **34**, produced by string substitution on the source exactly as `routing_fixtures.py:81-97` does:

`good`, `wrong_model`, `wrong_effort`, `null_effort`, `wrong_source`, `wrong_sandbox`, `missing_network_access`, `workspace_write_sandbox`, `wrong_cwd`, `approval_policy_on_request`, `wrong_reviewer`, `no_session_id`, `foreign_thread_events`, `foreign_turn_events`, `no_usage`, `usage_wrong_turn`, `usage_before_completion`, `completion_before_usage`, `approval_request`, `tool_call_request`, `turn_failed`, `turn_interrupted`, `no_terminal`, `server_error_notification`, `hang`, `oversized_rpc_line`, `notification_flood`, `duplicate_json_keys`, `garbage_line`, `orphan_grandchild`, `old_cli_version`, `unparseable_version`, `bad_initialize`, `wrong_codex_home`.

Five of these are new and deliberately narrow:

- **`missing_network_access`** — `ThreadStartResponse.sandbox` is `{"type":"readOnly"}` with the `networkAccess` key **entirely absent from the wire**, which is exactly what `ReadOnlySandboxPolicy` permits (`required: ["type"]`, `"default": false`). Everything else in the response is correct. It stays a *separate* scenario from **`wrong_sandbox`**, which keeps `networkAccess:false` and sends a wrong `sandbox.type`: the two prove different rows of the §3.6 table — `sandbox_network_not_proven` versus `sandbox_mismatch` — and must not be collapsed.
- **`usage_before_completion`** — emits `thread/tokenUsage/updated` and *then* the terminal `turn/completed`. This is the order `good` also uses.
- **`completion_before_usage`** — emits the terminal `turn/completed` **first** and `thread/tokenUsage/updated` afterwards, with a short bounded gap and without closing the stream in between. The drain must keep reading past the terminal notification and still succeed.
- **`oversized_rpc_line`** — one well-formed, correctly scoped notification whose single line exceeds `MAX_LINE` (1 MiB).
- **`notification_flood`** — more than `MAX_NOTIFICATIONS` (4096) small, correctly scoped, non-terminal notifications before any terminal or usage notification.

Helpers:

| Symbol | Purpose |
|---|---|
| `fake_native(tmp)` | copies `/usr/bin/python3.13` (a real ELF, `+x`, ~6.8 MiB) to serve as the fake Codex executable |
| `probe_anchor(tmp, *, variant='valid')` | creates `tmp/probe-anchor/` — a directory holding **only** the anchor — and writes `anchor.json`. `'valid'` writes exactly `canonical(PROBE_ANCHOR_STUB)` at mode `0400`; `'wrong_json'` writes a syntactically valid JSON object that is not the stub; `'garbage'` writes non-JSON bytes; `'oversize'` writes more than `PROBE_ANCHOR_MAX_BYTES`. Returns the absolute path |
| `adapter_spec(tmp, scenario, *, timeout=10, anchor='valid', anchor_pair=True)` | builds the pinned spec: `argv=[python3.13,'-I','-S',luna_codex_adapter.py, fake_native, '-I','-S', fake_server_py, scenario, '--probe-anchor', probe_anchor(tmp, variant=anchor)]`, `pins` over every absolute item — python, adapter, `fake_native`, `fake_server_py` **and the anchor** — `native_pins=[fake_native]`, `evidence_kind='live'`. `anchor_pair=False` drops the trailing `--probe-anchor <path>` pair, and the anchor pin with it, to produce the malformed-layout spec B28 needs |
| `oversized_stdout_spec(tmp)` | a separate pinned Python adapter stub used only for B24; it reads stdin and writes `MAX_OUTPUT+1` bytes directly to **its own stdout**. It does not launch the fake app-server or `luna_codex_adapter.py`, so the controller's `adapter_output_budget` is exercised causally rather than being confused with the Luna adapter's RX budget |
| `stage(tmp)` / `registry(tmp)` | read-only stage + shipped registry with an appended `luna_max_read_only` route |
| `packet(stage, receipt, nonce)` | schema-valid read-only worker packet via `stage_contracts.worker_packet` |
| `sidecar(tmp)` | parses what the fake recorded |

**The anchor is the final argv item in the fixtures too, and that is load-bearing.** `harness_adapter.readiness` computes the probe cwd from the *unbound* spec argv (`harness_adapter.py:93`), so with the anchor last it resolves to `tmp/probe-anchor/` in the tests and `~/.hermes/pilot/probe-anchor/` in production — the same derivation, deterministically, in both. A fixture ending in a bare scenario token would make `Path('good').parent == Path('.')` and let C9 pass by inheriting the test runner's cwd; the anchor is what makes C9 causal. `--probe-anchor` is a relative token and unpinned; the anchor path is absolute and pinned, so `bind_command` aliases it to `/proc/self/fd/<n>` at mode `0400`, `_validate_anchor` reads it through that alias, and the pair is then stripped and the anchor fd excluded from `pass_fds`.

Because the fake native is `python3.13` and the prefix supplies `-I -S <fake_server_py>`, the adapter's spawn becomes `python3.13 -I -S fake_server.py <scenario> app-server` — a real ELF exec through a real sealed memfd alias, with a real JSON-RPC peer on the other end. The scenario token reaches the fake; the anchor pair never does. No part of the adapter is stubbed.

### 8.2 Slice B — `scripts/tests/test_luna_adapter_protocol.py`

**RED** this file, then **GREEN** `scripts/luna_codex_adapter.py`.

```bash
python3 -B -m unittest discover -s scripts/tests -p test_luna_adapter_protocol.py -v
```

Everything goes through the real `harness_adapter.run_command` / `harness_adapter.execute`, so budgets, env, pinning, sealing and cleanup are exercised for real.

**46 tests, B1–B46**: 29 negative (B1–B29), 17 positive and invariant (B30–B46).

*Negative first*

| # | Test | Asserts |
|---|---|---|
| B1 | `test_model_mismatch_fails_closed` | empty stdout, exit ≠ 0 |
| B2 | `test_effort_mismatch_fails_closed` | " |
| B3 | `test_null_reasoning_effort_fails_closed` | " |
| B4 | `test_thread_source_mismatch_fails_closed` | " |
| B5 | `test_sandbox_workspace_write_fails_closed` | " |
| B6 | `test_sandbox_shape_mismatch_fails_closed` | `wrong_sandbox` — `sandbox.type` is not `readOnly` while `networkAccess` is correctly `false` → `sandbox_mismatch` |
| B7 | `test_missing_network_access_fails_closed` | `missing_network_access` — `sandbox` is `{"type":"readOnly"}` with the key **absent**, a wire shape `ReadOnlySandboxPolicy` explicitly permits → `sandbox_network_not_proven`, empty stdout, exit ≠ 0. Asserts the adapter does **not** fall back to the schema `"default": false`, does not infer from `sandbox.type`, and never emits `permissions.network` from an unobserved field. Distinct from B6 by construction |
| B8 | `test_cwd_mismatch_fails_closed` | " |
| B9 | `test_approval_policy_echo_mismatch_fails_closed` | " |
| B10 | `test_reviewer_mismatch_fails_closed` | " |
| B11 | `test_missing_session_id_fails_closed` | " |
| B12 | `test_foreign_thread_notifications_ignored` | `turn_not_completed`; foreign usage not adopted |
| B13 | `test_foreign_turn_usage_not_adopted` | `usage_evidence_missing` |
| B14 | `test_missing_usage_fails_closed` | never emits `{0,0}` |
| B15 | `test_approval_request_declined_and_fails_closed` | sidecar shows a JSON-RPC error reply **and** a subsequent `turn/interrupt` |
| B16 | `test_tool_call_request_declined_and_fails_closed` | same |
| B17 | `test_turn_failed_status_fails_closed` | |
| B18 | `test_turn_interrupted_status_fails_closed` | |
| B19 | `test_no_terminal_notification_fails_closed` | `turn_not_completed` |
| B20 | `test_server_error_notification_fails_closed` | |
| B21 | `test_timeout_kills_child` | `hang` + `timeout_seconds=1` → `ContractError('adapter_timeout')` |
| B22 | `test_rx_line_budget_enforced` | `oversized_rpc_line` → **adapter-side** `rx_budget`: empty stdout, non-zero exit, surfacing at the controller as `worker_exit_failure`, **not** as `adapter_output_budget` (§3.5) |
| B23 | `test_rx_notification_flood_enforced` | `notification_flood` → adapter-side `rx_budget` via `MAX_NOTIFICATIONS`, same observable shape as B22; the run ends bounded rather than draining forever |
| B24 | `test_harness_output_budget_enforced` | `oversized_stdout_spec(tmp)` writes `MAX_OUTPUT+1` bytes directly from the adapter process to the controller → **controller-side** `ContractError('adapter_output_budget')` (`harness_adapter.py:17,55`). No fake app-server is involved. Asserted alongside B22/B23 so adapter RX line/flood refusals and controller stdout overflow are three distinct outcomes |
| B25 | `test_harness_input_budget_enforced` | request > 128 KiB → `adapter_input_budget` |
| B26 | `test_duplicate_json_keys_rejected` | |
| B27 | `test_garbage_line_rejected` | |
| B28 | `test_missing_probe_anchor_pair_refused` | `adapter_spec(..., anchor_pair=False)` → `argv_shape`, empty stdout, exit ≠ 0. The adapter never treats a stray trailing argument as tolerable |
| B29 | `test_invalid_probe_anchor_refused` | parametrised over `anchor='wrong_json' | 'garbage' | 'oversize'` → `probe_anchor_invalid`, empty stdout, exit ≠ 0 |

*Positive and invariants*

| # | Test | Asserts |
|---|---|---|
| B30 | `test_happy_path_run_envelope` | `harness_adapter.execute` returns; body key set exactly `{protocol,operation,evidence_kind,proof,usage}`; proof key set == `PROOF_FIELDS`; `packet_hash == digest(packet)`; `nonce` copied; `permissions == {'mode':'read_only','allowed_paths':[],'network':False}`; `exit_code == 0`; usage ints equal the fake's `total`; `evidence_kind == 'live'` |
| B31 | `test_initialized_handshake_precedes_thread_start` | sidecar method order begins `['initialize','initialized','thread/start','turn/start']`; the recorded `initialized_seen` flag is `True` at `thread/start`. Causal, not declarative: the fake answers any pre-handshake request with `-32002 not_initialized`, so an adapter that skipped the notification could not reach a successful envelope at all |
| B32 | `test_usage_before_completion_drains_both` | `usage_before_completion` → success; both evidence items observed; usage equals the fake's `total` |
| B33 | `test_completion_before_usage_drains_both` | `completion_before_usage` → success. The drain does **not** stop at the terminal `turn/completed`; it keeps reading under the one shared monotonic deadline until the matching `thread/tokenUsage/updated` arrives, and the emitted usage equals the fake's `total`. Together with B14 and B19 this closes the order matrix: usage-then-terminal, terminal-then-usage, terminal-without-usage, usage-without-terminal |
| B34 | `test_session_id_binds_thread_session_and_turn` | all three ids present, `1 <= len <= 128` |
| B35 | `test_canonical_matches_schema_validation` | adapter `canonical` byte-identical to `schema_validation.canonical` over ≥5 packets incl. non-ASCII and nested objects |
| B36 | `test_prompt_derived_only_from_packet` | prompt contains `objective`, a `forbidden` entry and a `stop_conditions` entry; asserts **absence** of `nonce`, `packet_hash`, `route_receipt_hash`, `stage_hash`, route ids, the workspace absolute path and the probe-anchor path |
| B37 | `test_prompt_budget_enforced` | oversize `selected_context` → `prompt_budget`, empty stdout |
| B38 | `test_child_environment_is_strict` | sidecar env ⊆ `{PATH,HOME,CODEX_HOME,LANG,LC_ALL,TERM,TMPDIR,LC_CTYPE}` (`LC_CTYPE` tolerated as libc-injected, per `test_correction_runtime.py:135`); `HOME == pwd.getpwuid(os.getuid()).pw_dir`; `CODEX_HOME == HOME + '/.codex'` |
| B39 | `test_no_secret_reaches_child` | with `OPENAI_API_KEY`, `CODEX_API_KEY`, `ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY`, `GITHUB_TOKEN`, `GH_TOKEN`, `HERMES_GATEWAY_URL`, `AWS_SECRET_ACCESS_KEY`, `HTTP_PROXY`, `PYTHONPATH`, `NODE_OPTIONS`, `SSH_AUTH_SOCK` set in the controller process, **none** appears in the grandchild env |
| B40 | `test_environ_copy_absent_from_source` | `'os.environ.copy'` and `'environ.copy()'` do not occur in `scripts/luna_codex_adapter.py` |
| B41 | `test_no_start_new_session_in_source` | `'start_new_session'` does not occur in the adapter source |
| B42 | `test_fd_alias_passed_through_to_grandchild` | sidecar reports `/proc/self/exe` resolving to a `memfd:hermes-pinned` object; the anchor fd is **not** among the grandchild's inherited fds |
| B43 | `test_process_group_cleanup` | `orphan_grandchild`: fake spawns a long-lived grandchild writing a pidfile; after `run_command` returns, neither pid exists (bounded poll) |
| B44 | `test_stdout_is_exactly_one_json_object` | `stdout.count(b'\n') <= 1`, parses, no trailing bytes |
| B45 | `test_failure_emits_no_stdout` | parametrised over every negative **Luna adapter** scenario plus both anchor-failure specs of B28/B29: `stdout == b''`. B24's deliberately oversized standalone adapter stub is excluded because producing oversized stdout is the behavior under test there |
| B46 | `test_run_declines_before_any_write` | `orphan_grandchild`/`approval_request` scenarios leave the workspace tree digest unchanged |

### 8.3 Slice C — `scripts/tests/test_luna_adapter_probe.py` (offline probe proof)

```bash
python3 -B -m unittest discover -s scripts/tests -p test_luna_adapter_probe.py -v
```

Goes through the real `harness_adapter.readiness()` with the fake native ELF. **14 tests, C1–C14.**

| # | Test | Asserts |
|---|---|---|
| C1 | `test_probe_ready_identity` | `ready True`; `identity == {'harness':'luna','model':'gpt-5.6-luna','effort':'max'}`; `supported_modes == ['read_only']`; `evidence_kind == 'live'`; `adapter_digest == digest(spec)` |
| C2 | `test_probe_envelope_key_set_exact` | exactly `{protocol,operation,ready,identity,supported_modes,evidence_kind}` |
| C3 | `test_probe_rejects_old_cli_version` | `probe_failed_or_mismatched` |
| C4 | `test_probe_rejects_unparseable_version` | " |
| C5 | `test_probe_rejects_bad_initialize_response` | " |
| C6 | `test_probe_rejects_wrong_codex_home` | " |
| C7 | `test_probe_identity_mismatch_when_route_model_differs` | `identity_mismatch` path |
| C8 | `test_probe_evidence_kind_mismatch` | `evidence_mode='synthetic'` vs a `live` adapter → `adapter_evidence_mismatch` |
| C9 | `test_probe_cwd_is_the_probe_anchor_directory` | sidecar `cwd` equals `Path(spec['argv'][-1]).parent` — i.e. `tmp/probe-anchor/` — **exactly**, and is not under `stage['workspace']` and not under any Codex package tree. Causal because the anchor is the final argv item; it cannot pass by inheriting the test runner's cwd |
| C10 | `test_probe_method_list_is_handshake_only` | sidecar method list == `['initialize','initialized']` — the `initialize` request plus the mandatory `ClientNotification`, in that order, and nothing else. No `thread/start`, no `turn/start`, no model call. This is the probe-path half of B31 |
| C11 | `test_probe_never_raises_out_of_readiness` | every failure degrades to `ready:False` |
| C12 | `test_probe_anchor_invalid_degrades_to_not_ready` | `anchor='garbage'` and `anchor_pair=False` both yield `ready:False` out of `readiness()` with no exception escaping, and the fake's sidecar is absent — the adapter refuses on argv/anchor shape *before* spawning Codex |
| C13 | `test_readiness_gate_requires_approved_for_pilot` | an `unverified` route is never probed (sidecar absent) |
| C14 | `test_shipped_registry_still_has_no_ready_luna_route` | loads `assets/executor-routes.json` unchanged; `executor_routes.available(...) == ['owner']` |

### 8.4 Why this is a real proof and where it stops

The fake is a genuine peer process reached through a genuine sealed memfd exec, over a genuine JSON-RPC transport, driven by the genuine controller. What it does **not** prove: that the real `codex app-server` populates `reasoningEffort`, honours `config.model_reasoning_effort`, or reports `sandbox` in the shape captured from its own schema generator. Those are proven only once, by §10.

---

## 9. TDD Slice D — docs, version, pilot tooling

- `references/luna-adapter.md` — invocation contract, constants table, verification table, failure codes, the `network:false` caveat, and the offline/live boundary.
- `references/adapter-protocol.md` — append a `native_pins` section (§2d) and a Codex section naming `codex-cli 0.153.4` and the frozen method set.
- `references/verification.md` — prepend a boundary block: synthetic fake-app-server + offline probe only; no live executor harness; no Luna admission; `assets/executor-routes.json` untouched.
- `SKILL.md` — add `## Версия 1.4: адаптер Codex (Luna)` linking `references/luna-adapter.md`.
- `README.md` — version line.
- `tools/pilot_registry.py`, `tools/pilot_preflight.py` (§4, §10).

**Version — 1.3.0 → 1.4.0.** The skill artifact does change: `scripts/pinned_runtime.py`, `scripts/stage_contracts.py`, `assets/schemas/executor-registry.schema.json`, plus two new installed files (`scripts/luna_codex_adapter.py`, `references/luna-adapter.md`). A schema property is added and the sealed-execution boundary is widened — exactly what a minor bump is for.

**All eight literal sites** — the count established in §1a, which the earlier seven-item list dropped:

| # | File:line | Literal |
|---|---|---|
| 1 | `SKILL.md:4` | `version: 1.4.0` |
| 2 | `README.md:47` | `The installed skill version is **1.4.0**.` |
| 3 | `scripts/install_skill.py:16` | `VERSION = "1.4.0"` |
| 4 | `scripts/bounded_runtime.py:27` | `SKILL_VERSION = "1.4.0"` |
| 5 | `scripts/route_cli.py:98` | `'candidate_version':'1.4.0'` |
| 6 | `scripts/tests/test_bundle_skill_version.py:27` | `MANIFEST_VERSION = "1.4.0"` |
| 7 | `scripts/tests/test_skill_contract.py:19` | `assertIn("version: 1.4.0", text)` |
| 8 | **`scripts/tests/test_route_cli.py:46-48`** | `test_candidate_version` pins **three** independent literals: `bounded_runtime.SKILL_VERSION == '1.4.0'` (`:46`), `install_skill.VERSION == '1.4.0'` (`:47`), `assertIn('version: 1.4.0', SKILL.md)` (`:48`) |

Site 8 is not optional bookkeeping: `test_route_cli.test_candidate_version` asserts the *old* literal, so bumping sites 3–4 without it leaves the suite RED and makes acceptance criterion 1 unreachable. `scripts/tests/test_route_cli.py` is deliberately absent from the do-not-modify list in **Global Constraints**.

Prose mentioning the version outside these eight sites (for example the `Версия **1.3.0**` sentence in `SKILL.md`) is documentation, not a pinned literal; the new `## Версия 1.4` section above supersedes it and no test asserts it.

`scripts/tests/test_skill_contract.py` will fail until `references/luna-adapter.md` exists and every relative link resolves — that is the intended RED for this slice.

---

## 10. Live pilot — private, disposable, read-only, zero writes

**Not part of the implementation session.** A separate, human-initiated run.

### 10.0 Blocker (open as of this plan)

At **2026-09-19T00:12:39+03:00** the installed Codex reported a usage limit with **retry after 11:09 AM**. The pilot cannot start before **2026-09-19T11:09+03:00**. Clear it with no model call:

```bash
python3 -B tools/pilot_preflight.py --codex "$CODEX_ELF"          # initialize + account/rateLimits/read
```

Expected: non-exhausted primary and secondary windows. If still limited: **stop**. Do not retry in a loop, do not switch models, do not proceed on a partial window.

### 10.1 Step 0 — freeze the CLI, confirm the effort key, **observe the sandbox** (no model call)

```bash
CODEX_ELF=/home/ivan/.local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex
codex --version                                   # expect: codex-cli 0.153.4
sha256sum "$CODEX_ELF"                            # → codex_sha256
stat -c '%s %Y' "$CODEX_ELF"
codex app-server generate-json-schema --out ~/.hermes/pilot/schema-0.153.4/
python3 -B tools/pilot_preflight.py --codex "$CODEX_ELF" --dump-config-requirements
python3 -B tools/pilot_preflight.py --codex "$CODEX_ELF" --observe-thread-start \
        --out ~/.hermes/pilot/thread-start-observation.json
```

Confirm `model_reasoning_effort` is the accepted `config` key. If it is not: correct `EFFORT_CONFIG_KEY`, re-run the full suite, re-pin, and restart from Step 0.

**The `--observe-thread-start` gate — the pilot does not start until the real server has been seen to report these values.** The step performs `initialize` → `initialized` → `thread/start` with exactly the §3.6 step 3 parameters (`ephemeral: true`, `sandbox: 'read-only'`) and then closes the thread. It issues **no `turn/start`, and therefore no model call** — a thread is created, no turn is executed, no tokens are spent. It writes the raw `ThreadStartResponse` to the observation file and exits non-zero unless **every** row holds:

| Observed | Required | If it does not hold |
|---|---|---|
| `sandbox.type` | `'readOnly'` | stop |
| **`sandbox.networkAccess`** | **present on the wire and exactly the JSON literal `false`** (checked with `is False`; a missing key, `null`, `0` or `''` all fail) | **stop — `luna_max_read_only` is never admitted.** The schema `"default": false` is not evidence and is never substituted. There is no fallback and no config flag that relaxes this |
| `reasoningEffort` | `'max'`, present and non-null | stop |
| `thread.model` | present, non-null, `'gpt-5.6-luna'` | stop |
| `thread.reasoningEffort` | present, non-null, `'max'` | stop |
| `thread.threadSource` | present, non-null, `'user'` | stop |
| `approvalPolicy` / `approvalsReviewer` | `'never'` / `'user'` | stop |

These are the same rows the adapter enforces at run time (§3.6 step 4); observing them first means a refusal surfaces here, before any registry is written, rather than as a consumed pilot attempt. "Stop" means exactly that: no retry loop, no model switch, no weakening of the check, no registry write. The observation file goes into the evidence bundle either way, including when it records the refusal.

### 10.2 Step 1 — disposable workspace

```bash
W=$(mktemp -d /tmp/hermes-luna-pilot.XXXXXX)
printf 'def add(a, b):\n    return a + b\n' > "$W/sample.py"
sha256sum "$W/sample.py"
```

Public-redacted, throwaway, no repo content, no secrets. Deleted afterwards.

### 10.3 Step 2 — private registry and approval

```bash
mkdir -p ~/.hermes/pilot/probe-anchor && chmod 700 ~/.hermes/pilot
ANCHOR=~/.hermes/pilot/probe-anchor/anchor.json

# 1. Create/verify the anchor stub and print the real digests. Writes no registry.
python3 -B tools/pilot_registry.py \
  --codex   "$CODEX_ELF" \
  --python  /usr/bin/python3.13 \
  --adapter "$PWD/scripts/luna_codex_adapter.py" \
  --probe-anchor "$ANCHOR" \
  --bindings-only

# 2. Fill the approval record from those printed values — never from guesses.
$EDITOR ~/.hermes/pilot/luna-max-approval.json

# 3. Validate the record field-by-field (§4.1) and only then write the registry.
python3 -B tools/pilot_registry.py \
  --codex   "$CODEX_ELF" \
  --python  /usr/bin/python3.13 \
  --adapter "$PWD/scripts/luna_codex_adapter.py" \
  --probe-anchor "$ANCHOR" \
  --approval ~/.hermes/pilot/luna-max-approval.json \
  --out     ~/.hermes/pilot/luna-max-readonly-registry.json
REG=~/.hermes/pilot/luna-max-readonly-registry.json
REG_SHA=$(sha256sum "$REG" | cut -d' ' -f1)

# The written route's argv must END in the anchor, and pins must cover all four objects.
python3 -B - "$REG" "$ANCHOR" <<'EOF'
import json,sys
r=next(x for x in json.load(open(sys.argv[1]))['routes'] if x['route_id']=='luna_max_read_only')
a=r['adapter']
assert a['argv'][-2:]==['--probe-anchor',sys.argv[2]], a['argv']
assert len(a['pins'])==4 and sys.argv[2] in a['pins'], sorted(a['pins'])
assert a['native_pins']==[x for x in a['argv'] if x.endswith('/codex')], a['native_pins']
print('anchor + four pins OK')
EOF

git diff --exit-code assets/executor-routes.json      # MUST be clean
```

Step 3 refuses without writing if any §4.1 field mismatches, if the anchor is not exactly the stub, or if `--probe-anchor` is not absolute and outside the repo/worktree.

### 10.4 Step 3 — stage envelope

`~/.hermes/pilot/luna-max-readonly-stage.json`:

- `mode: "read_only"`, `allowed_paths: []`, `role: "review"`
- `risk: {level:"low", production:false, secrets:false, irreversible:false, money:false, publication:false, architecture_change:false}`
- `executor_candidates: ["luna_max_read_only","owner"]`
- `data_policy: {"classification":"public-redacted","external_allowed":true}` — `live` refuses `synthetic`, and `available()` refuses `private`
- `features: {context_rerank:false, semantic_cascade:false, stage_transition:false}` — so `_admit_gates` never fires and no gate evidence is needed
- `limits: {max_jev_calls:0, max_worker_calls:1, max_seconds:300, max_context_chars:4000}`
- `inputs: [{id:"sample", path:"sample.py", sha256:<actual>}]`
- `output_contract: {required_paths:["sample.py"], allow_delete:false, max_changed_files:0}`
- `checks`: one pinned command re-verifying `sample.py`'s SHA-256

`required_paths` must be an input path: `read_only` forces `allowed_paths:[]`, and `required_paths ⊆ allowed_paths ∪ input paths`. **The pilot therefore proves identity, session, sandbox, terminal status and usage. It does not prove artifact production.** That sentence goes into the evidence bundle verbatim.

### 10.5 Step 4 — run

```bash
STAGE=~/.hermes/pilot/luna-max-readonly-stage.json
RUN=~/.hermes/workspaces/luna-pilot-$(date -u +%Y%m%dT%H%M%SZ)

python3 -B scripts/route_cli.py route-validate --stage "$STAGE" --registry "$REG" --registry-sha256 "$REG_SHA"
python3 -B scripts/route_cli.py route-init     --stage "$STAGE" --registry "$REG" --registry-sha256 "$REG_SHA" \
                                               --evidence-mode live --run "$RUN"
python3 -B scripts/route_cli.py route-advance  --run "$RUN" --fixed-route luna_max_read_only
python3 -B scripts/route_cli.py route-inspect  --run "$RUN"
```

`--fixed-route` bypasses Jev entirely. No `--allow-typesafe`, no grants. Exactly **one** attempt. On `needs_review` or a non-zero exit: stop, capture, do not retry.

### 10.6 Evidence bundle — `~/.hermes/pilot/evidence-<UTC>/`

| File | Content |
|---|---|
| `cli-identity.json` | `codex --version`, `codex_sha256`, `st_size`, `st_mtime_ns`, npm package path |
| `schema-0.153.4.sha256` | digest over the generated protocol schema directory |
| `thread-start-observation.json` | the raw `ThreadStartResponse` from §10.1 `--observe-thread-start`, including the observed `sandbox.networkAccess`. Kept whether it passed or refused; no `turn/start`, no model call |
| `probe-anchor.json` + `probe-anchor.sha256` | the anchor stub and its digest, matching the fourth pin and the final `argv` item of the written route |
| `rate-limits-before.json` / `rate-limits-after.json` | `account/rateLimits/read` (no model call) |
| `approval.json` + `approval.sha256` | the human-written approval record (§4.1) and its digest, plus the tool's field-by-field validation output |
| `registry.json` + `registry.sha256` | the private pilot registry, plus proof `assets/executor-routes.json` is unchanged |
| `stage.json`, `seal.json`, `route-receipt.json`, `worker-packet.json` | copied from `$RUN` |
| `result.json` | `$RUN/attempts/1/result.json` — `proof`, `worker_usage`, `evidence_kind:"live"`, `changed_paths:[]`, `scope_violations:[]`, `artifacts` |
| `proof.json` | the eight `PROOF_FIELDS`, with `model=gpt-5.6-luna`, `effort=max`, the composite `session_id` |
| `usage.json` | the emitted `input_tokens`/`output_tokens` plus the raw `ThreadTokenUsage` for cross-check |
| `tree-before.sha256` / `tree-after.sha256` | workspace digests — must be **identical** |
| `test-suite.txt` | full `unittest discover` output: zero failures, zero skips |
| `boundary.md` | what this does and does **not** prove |

`boundary.md` must state, in substance: one read-only turn, one model, one machine, one CLI build; no write benchmark; no artifact production; no concurrency, quota, cost or latency characterisation; no OS-sandbox claim; `network:false` describes worker tool permissions, **not** a claim that the provider can be reached without transport; the shipped registry was never activated.

### 10.7 Teardown

```bash
rm -rf "$W"
chmod -R go-rwx ~/.hermes/pilot
git status --porcelain            # only the intended source changes
```

Keep `$RUN` and the evidence bundle. `~/.hermes/pilot/` stays out of git.

**Route activation is not part of this plan.** Flipping any route in `assets/executor-routes.json` is a separate, separately reviewed change that consumes this bundle as its input.

---

## 11. Implementation order

1. **Slice A** — `test_native_pins.py` RED (**A1–A14**) → schema + `validate_pins` + `bind_command` GREEN. Full suite green.
2. **Slice B** — `luna_fixtures.py` (34 fake-app-server scenarios, `probe_anchor`, `adapter_spec` and standalone `oversized_stdout_spec` helpers) + `test_luna_adapter_protocol.py` RED (**B1–B46**, the 29 negatives first) → `scripts/luna_codex_adapter.py` GREEN. Full suite green.
3. **Slice C** — `test_luna_adapter_probe.py` RED (**C1–C14**) → probe path GREEN. Full suite green.
4. **Slice D** — docs, `SKILL.md`, `README.md`, **the eight version-literal sites of §9 including `scripts/tests/test_route_cli.py:46-48`**, `tools/pilot_registry.py` (with `--probe-anchor`, `--bindings-only` and the §4.1 validation), `tools/pilot_preflight.py` (with `--dump-config-requirements` and `--observe-thread-start`). Full suite green.
5. **Commit and stop.** §10 is a separate human-initiated session after the 11:09 AM blocker clears.

### Commands

```bash
cd /home/ivan/worktrees/hermes-luna-adapter

# RED, one slice at a time (expect failures naming the missing module/symbol)
python3 -B -m unittest discover -s scripts/tests -p test_native_pins.py -v
python3 -B -m unittest discover -s scripts/tests -p test_luna_adapter_protocol.py -v
python3 -B -m unittest discover -s scripts/tests -p test_luna_adapter_probe.py -v

# GREEN — the whole suite, zero skips
python3 -B -m unittest discover -s scripts/tests -v

# Diagnostics
python3 -B scripts/stage_cli.py doctor
python3 -B scripts/route_cli.py route-doctor        # candidate_version 1.4.0, live_readiness_claim false
```

---

## 12. Acceptance criteria

1. `python3 -B -m unittest discover -s scripts/tests -v` passes with **zero skips** on Python 3.13.5 / Linux. The three new files contribute **74 tests**: A1–A14 (14), B1–B46 (46), C1–C14 (14).
2. `git diff --exit-code assets/executor-routes.json` is clean. Every real executor route is still `unverified`, `adapter:null`, `model:null`.
3. `scripts/tests/test_bounded_loop.py` (incl. `test_luna_routes_remain_unavailable_in_loop`, `:243`) and `scripts/tests/test_executor_contracts.py` pass **unmodified**.
4. `python3 -B scripts/route_cli.py route-doctor` reports `candidate_version: "1.4.0"`, `network_checked: false`, `live_readiness_claim: false`.
5. `scripts/luna_codex_adapter.py` imports only stdlib — `grep -nE '^\s*(from|import)\s+(stage_contracts|schema_validation|executor_[a-z]+|harness_adapter|pinned_runtime|runtime_support|bounded_)' scripts/luna_codex_adapter.py` finds nothing.
6. Neither `os.environ.copy` nor `start_new_session` occurs in `scripts/luna_codex_adapter.py`.
7. Every negative scenario produces **empty stdout** and a non-zero exit. No test ever observes a fabricated `{"input_tokens":0,"output_tokens":0}`.
8. The probe issues `initialize` plus the `initialized` notification and nothing else — no `thread/start`, no `turn/start`, no model call — asserted from the fake's recorded method list (C10), with the handshake enforced causally by the fake (B31).
9. An absent `sandbox.networkAccess` refuses (B7) and is observed as a different outcome from a wrong `sandbox.type` (B6). Both terminal-vs-usage arrival orders succeed (B32, B33). The adapter's `rx_budget` (B22, B23) and the controller's `adapter_output_budget` (B24) are observed as three distinct outcomes.
10. Every adapter spec in the plan — the §4 pilot route and the §8.1 fixture alike — ends in the pinned probe anchor, and the anchor is one of its pins. The probe cwd equals the anchor's directory (C9); a missing pair or a non-stub anchor refuses (B28, B29, C12).
11. All **eight** version-literal sites of §9 read `1.4.0`, including `scripts/tests/test_route_cli.py:46-48`.
12. `native_pins` is optional: every pre-existing adapter spec in `routing_fixtures.py` and `loop_fixtures.py` behaves byte-identically (A12), and the native validation matrix is the closed two-code set of §2b (A13).
13. No new dependency. No file outside §5 is modified.
14. This plan is committed at `docs/superpowers/plans/2026-09-19-luna-codex-adapter.md`.

---

## 13. Known risks — stated, not hidden

| Risk | Handling |
|---|---|
| Codex ELF is at 96.4 % of `MAX_EXECUTABLE`; the next release breaks pinning | Test A9 pins the failure to the legible `executable_size`. Raising the cap is a separate reviewed change. |
| ~247 MiB memfd copy + 2 SHA-256 passes per `run_command`, twice per attempt | Documented in `references/adapter-protocol.md`. Not optimised in this slice. |
| Adapter `timeout_seconds` capped at 120 s while effort `max` can exceed it | The pilot uses a deliberately trivial prompt. `adapter_timeout` fails the pilot closed — correct behaviour, not a defect. Raising the cap is a non-goal. |
| `EFFORT_CONFIG_KEY` could not be proven offline | Verified against `ThreadStartResponse.reasoningEffort`; a wrong key fails closed (`effort_not_proven`) rather than silently running at default effort. §10.1 confirms it before the pilot. |
| `thread/start` has no `effort` parameter in 0.153.4 | Effort set on `turn/start` **and** `thread/start.config`; proven from the response echo, not from the request. |
| **`ThreadStartResponse.sandbox.networkAccess` may be absent on the wire** — `ReadOnlySandboxPolicy` has `required: ["type"]` and the field carries `"default": false` | The adapter requires it present and `is False` (§3.6); a default is not an observation. **§10.1 `--observe-thread-start` gates the pilot on seeing it**, so an absent field stops before any registry write and `luna_max_read_only` is simply never admitted. B7 proves the refusal offline. No fallback, no inference from `sandbox.type`, no relaxing flag. |
| **`Thread.model`, `Thread.reasoningEffort` and `Thread.threadSource` are optional and nullable in 0.153.4**, yet §3.6 requires all three | Deliberate fail-closed choice: a thread that will not restate its own identity yields no proof. A real server omitting any of them refuses the run rather than degrading it. **§10.1 observes all three before the pilot**, so this surfaces as a pre-pilot stop, not a consumed attempt. Synthetic coverage is B3/B4 and the `null_effort` / `wrong_source` scenarios. |
| **`reasoningEffort` is configured thread state, not per-turn execution telemetry** — the 0.153.4 schema says so verbatim (§1a) | `proof.effort` therefore attests *the effort the thread was configured with*, not a measurement that the turn executed at that effort. Nothing in this slice can close that gap; it is stated verbatim in `references/luna-adapter.md` and in the pilot `boundary.md` rather than papered over. Effort is set on `turn/start` **and** `thread/start.config` and proven from the response echo, never from the request. |
| A trusted adapter can lie about its own identity | Unchanged and unfixable at this layer (`references/adapter-protocol.md:16`). Mitigated by pinning, sealing, human approval, and using observed rather than requested values. |
| Codex may reach the network; `network:false` is a *worker tool permission*, not a transport claim | Restated verbatim in `references/luna-adapter.md` and in the pilot `boundary.md`. |
| The fake app-server may diverge from the real one | Acknowledged in §8.4. The frozen schema is regenerated and hashed in §10.1; divergence surfaces as a fail-closed refusal, never as a silent success. |
