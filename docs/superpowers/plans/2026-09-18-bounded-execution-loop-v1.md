# Bounded Execution Loop — Vertical Slice 1

> **Ownership note (agentic worker).** This plan is written to be executed by an agentic worker, not by a human reader skimming for intent. The worker owns the *execution* of the tasks below and nothing more: it implements exactly the named files and symbols, in the stated order, writing the listed tests RED before the corresponding implementation. It does not widen scope, invent additional modules, relax a contract to make a test pass, or mark a task complete on a partial result. Every non-goal in **Global Constraints** is binding. Architectural or scope ambiguity discovered mid-execution stops the task and returns to the parent with the facts — it is never resolved by guessing. Final acceptance of this work is parent-only; a green test suite is evidence, not acceptance.

## Goal

Add the missing **loop** layer to `hermes-bounded-stage`: a hash-bound chain of bounded iterations over parent-pre-admitted stages, with a parent checkpoint between iterations and an **immutable continuation** on redirect. Everything inside an iteration is delegated verbatim to the existing `executor_runtime`. The slice is complete when a synthetic end-to-end loop runs one bounded iteration, reaches a parent checkpoint, and a parent redirect produces a new continuation loop that carries only hash-verified progress while leaving the source loop's immutable history byte-identical.

## Architecture

```
                 parent (owns goal, budgets, closed stage menu, final acceptance)
                    │  loop-envelope.json  (1–8 pre-admitted stage envelopes)
                    ▼
  bounded_loop.py ──── loop.json / registry.json / loop-seal.json
     │   │             transitions/NNNN-<kind>.json   (create-only hash chain = truth)
     │   │             loop-state.json                (projection of the chain)
     │   │             iterations/<n>/run/            (immutable executor_runtime runs)
     │   │
     │   └── loop_menu.choose()  ← Jev picks ONE of: <stage_id> | return_to_parent | stop
     │                              (purpose 'stage_transition', existing outbound boundary)
     ▼
  executor_runtime.initialize/advance/transition/accept     ← unchanged, one run per iteration
     │
     ├── harness_adapter.execute()   one bounded worker attempt (pinned subprocess adapter)
     ├── deterministic pinned checks          ── always first
     ├── semantic_cascade.cascade()           ── only after deterministic success
     └── stage_transition.choose()            ── ≤1 bounded correction, parent-authorized
```

- **Loop = chain of pre-admitted stages.** Jev never authors a stage; it selects from a closed menu.
- **Iteration = exactly one `executor_runtime` run.** `stage-envelope.schema.json` caps `limits.max_worker_calls` at 2 and `worker-packet.schema.json` caps `execution.attempt` at 2, so an iteration holds at most one attempt plus one bounded correction. The loop grows iteration count, never correction count, and each iteration is a *different* pre-admitted stage.
- **State = a create-only hash-chained transition ledger.** `loop-state.json` is a derived projection with no independent authority; a state-only edit fails before any action runs.
- **Redirect = immutable continuation.** A new sealed loop directory bound by hash to the accepted evidence of the source; the source's immutable set is never rewritten.

## Tech Stack

- Python 3.13, standard library only. No new dependencies; none may be added.
- JSON contracts validated by `scripts/schema_validation.py` — a documented **strict subset** of JSON Schema 2020-12. Only the keywords in `schema_validation.KEYWORDS` (`schema_validation.py:15-18`) are legal, and `$ref` is legal **only** as `#/$defs/<name>` (`schema_validation.py:58-63`); no cross-file references.
- Tests: `unittest`, discovered from `scripts/tests`, using the existing `routing_fixtures` / `outbound_fixtures` synthetic harness.
- Canonical JSON (`schema_validation.canonical` / `digest`): UTF-8, sorted keys, compact separators, no NaN.
- Create-only writes: `runtime_support.create_file` (`runtime_support.py:78`, `O_CREAT|O_EXCL`). Atomic republication: `runtime_support.save_json` (`runtime_support.py:90`, mkstemp + `os.replace` + directory fsync), reached through `executor_runtime.save`.
- Concurrency: `fcntl.flock(LOCK_EX|LOCK_NB)` via `executor_runtime.lock`.

## Global Constraints

**Non-goals — none of these may be built, enabled, or partially prepared in this slice:**

- No global routing. No arbitrary tool planning. No production activation. No second runtime.
- No generic event-sourcing framework. The transition ledger is a fixed, closed set of seven loop-level record kinds with a hard cap of 32 records per loop — nothing else subscribes to it, replays it, or projects from it.
- No loosening of existing outbound / privacy / scope protections.
- All real executor routes stay `unverified` / `disabled`. `assets/executor-routes.json` is **not** edited.
- These files are **not** modified: `scripts/typed_jev.py`, `scripts/outbound_admission.py`, `scripts/executor_runtime.py`, `scripts/executor_routes.py`, `scripts/stage_transition.py`, `scripts/stage_contracts.py`, `scripts/harness_adapter.py`, `scripts/semantic_cascade.py`, `scripts/context_rerank.py`, `scripts/runtime_support.py`, `assets/schemas/stage-envelope.schema.json`, `assets/schemas/worker-packet.schema.json`, `assets/schemas/route-receipt.schema.json`, `assets/executor-routes.json`.
- In particular the continuation helper required by **Redirect** lives in `bounded_loop`, **not** in `executor_runtime`. No new parameter, helper or refactor is added to `executor_runtime.py`.
- `typed_jev.PURPOSES` and `outbound_admission.PURPOSES` gain no new member; the loop reuses the existing `stage_transition` purpose so the outbound boundary is untouched.
- Final acceptance stays parent-only. Every receipt and every ledger record this slice emits carries `parent_acceptance` const `false`.
- The initial end-to-end proof uses the **synthetic pinned adapter** and the **pinned deterministic action**. Live Jev is exercised **separately**, never in the same run as the adapter proof.

**Binding invariants:**

- Deterministic checks always precede semantic checks. A failed deterministic gate never reaches a semantic provider.
- **Lock discipline (exact).** The only two lock targets are `<loop_dir>/loop-seal.json` and `<workspace>/.staged-routing-workspace.json`. Acquisition order is always loop seal → workspace marker, never the reverse, never twice.
  - `bounded_loop.advance` holds **only** `loop-seal.json`. It must never hold the workspace marker, because `executor_runtime.initialize` (`executor_runtime.py:91`) and `executor_runtime.advance` (`executor_runtime.py:228`) each acquire it on a fresh fd; a nested acquisition in the same process fails `one_writer_lock_busy`.
  - `bounded_loop.initialize` and `bounded_loop.redirect` **do** hold the workspace marker, and therefore never call `executor_runtime.initialize`, `advance`, `accept` or `transition` while holding it. `executor_runtime.inspect` (`executor_runtime.py:169-174`) and `executor_runtime.snapshot` take no lock and are the only runtime calls allowed inside that section.
  - `bounded_loop.checkpoint` and `bounded_loop.accept` hold only `loop-seal.json`.
- Write-ahead status: `iteration_running` is committed to the ledger *before* `executor_runtime.initialize` and is never auto-resumed — the same write-ahead rule as `executor_runtime.py:232`.
- Recovery never re-executes a worker, a deterministic action, or a provider call. It only republishes a projection that the ledger already commits.
- Scope or architectural ambiguity returns to the parent; it never advances the loop.

---

## Context

`hermes-bounded-stage` v1.2.0 already delivers everything the approved product behavior asks for **inside a single stage run**: `executor_runtime.advance()` performs one bounded worker attempt through a pinned adapter, runs deterministic pinned checks, then (and only then) the semantic cascade; `executor_runtime.transition()` + `stage_transition.choose()` offer a closed whole-stage menu that can grant exactly one bounded correction; `executor_runtime.accept()` keeps final acceptance parent-only.

What does not exist is the **loop**: nothing links one sealed run to the next, there is no parent checkpoint between runs, and there is no way to redirect a goal while preserving verified progress. Today a redirect would mean editing a sealed run or starting from nothing.

This slice adds exactly that missing layer — a hash-bound chain of iterations over pre-admitted stages, with a parent checkpoint and an **immutable continuation** on redirect — and nothing else.

---

## Design

### The loop is a chain of pre-admitted stages

A parent seals a **loop envelope**: a goal, a workspace, budgets, and a closed, ordered list of 1–8 full stage envelopes it has already admitted. Jev never authors a stage. At each checkpoint it picks one item from a closed menu: `<stage_id>` of a remaining pre-admitted stage, `return_to_parent`, or `stop`. This is the same discipline as `executor_routes.request()` and `stage_transition.choose()`.

Each iteration is exactly one `executor_runtime` run over one selected stage. Because `stage-envelope.schema.json` caps `limits.max_worker_calls` at 2 and `worker-packet.schema.json` caps `execution.attempt` at 2, an iteration can contain at most one attempt plus one bounded correction — the loop cannot be used to buy more corrections. Only the number of iterations grows, and each iteration is a *different* pre-admitted stage.

### Pre-admitted stages are hash-pinned to files an earlier stage may rewrite

`stage-envelope.schema.json` requires `inputs` `minItems: 1`, and `executor_runtime.initialize` re-validates the stage with `verify_files=True` (`executor_runtime.py:76` → `stage_contracts.py:113-114`, reason `input_source_changed`). A later stage that pins a file an earlier stage is allowed to rewrite is therefore **dead on arrival at iteration N**, and pinning the post-mutation hash instead fails `validate_envelope` at seal time. This is structural, not incidental, so the loop envelope rejects it up front:

- `validate_envelope` refuses any envelope where, for `i < j`, a path in `stages[j].inputs[*].path` appears in `stages[i].allowed_paths` — reason `stage_input_mutated_by_earlier_stage`.
- `loop_fixtures` therefore gives stage B a **stable** input (`spec.md`) that no earlier stage may write, and a disjoint write scope (`notes.md`).

### State machine (loop level)

```
ready ──advance──► iteration_running ──(executor_runtime.advance)──► checkpoint_required
checkpoint_required ──checkpoint('continue')──► ready        [prior iteration must be parent_accepted]
checkpoint_required ──checkpoint('stop')────► stopped         (terminal)
checkpoint_required ──redirect(...)─────────► redirected      (terminal; new continuation loop = ready)
checkpoint_required ──accept(...)───────────► loop_accepted   (terminal)
```

`TERMINAL = {'redirected','stopped','loop_accepted'}`. `iteration_running` is committed **before** `executor_runtime.initialize` and is never auto-resumed: finding it on entry yields `checkpoint_required` with reason `interrupted_iteration_no_implicit_retry`.

`advance` is legal only from `ready`; from `checkpoint_required` it raises `checkpoint_required_before_advance`, and from any `TERMINAL` status `loop_terminal`.

`reason` carries why the parent is needed: `iteration_ready_for_parent_review`, `iteration_needs_review`, `scope_ambiguity_returned_to_parent`, `no_admissible_stage`, `interrupted_iteration_no_implicit_retry`.

### State integrity: a create-only hash-chained transition ledger

`loop-state.json` is a **projection**. Authority lives in `<loop_dir>/transitions/NNNN-<kind>.json`, written create-only with `runtime_support.create_file` and never rewritten.

Record kinds (closed set, seven): `genesis`, `iteration_started`, `iteration_recorded`, `interrupted`, `checkpoint`, `redirect`, `accept`.

```python
record = {'schema_version':1,'sequence':int,'kind':str,
          'previous_transition_hash': None | sha256,   # None only for genesis
          'seal_hash': sha256,                          # digest(seal), every record
          'before_state_hash': None | sha256,           # None only for genesis
          'after_state_hash': sha256,
          'status':str,'reason':str,'iteration':int,
          'evidence':{'stage_id':str|None,'run_relative':str|None,'result_hash':sha|None,
                      'tree_hash':sha|None,'accepted':bool,'receipt_hash':sha|None,
                      'approval_hash':sha|None,'continuation_hash':sha|None},
          'jev_delta':{'calls':int,'input_tokens':int,'output_tokens':int},
          'recorded_at':float,'parent_acceptance':False}
```

`_apply(state, record) -> state` is a **pure, total** function: every field it changes is derived from the record alone (`status`, `reason`, `iteration`, the appended/updated `iterations` entry from `evidence`, `last_selection` / `last_checkpoint` / `redirected_to` from `evidence`, and the `jev_calls` / `jev_usage` accumulators from `jev_delta`). `created_at`, `deadline` and `seal_hash` are never touched after genesis.

**Load algorithm** (`bounded_loop._load`, run before every action — `inspect`, `advance`, `checkpoint`, `redirect`, `accept`):

1. Read `loop.json`, `registry.json`, `loop-seal.json`, `loop-state.json` via `executor_runtime.read`.
2. `seal['loop_hash'] == digest(envelope)`, `seal['registry_hash'] == digest(registry)`, `state['seal_hash'] == digest(seal)` — else `sealed_loop_changed`.
3. `transitions/` must contain exactly the names `%04d-<kind>.json` for a contiguous `0..N-1`, `1 <= N <= 32`, each name's `<kind>` equal to the record's `kind` — else `transition_ledger_gap`.
4. Each record validates against `loop-transition.schema.json` — else `transition_ledger_shape`.
5. `records[0]`: `kind == 'genesis'`, `sequence == 0`, `previous_transition_hash is None`, `before_state_hash is None`, `seal_hash == digest(seal)` — else `transition_genesis_mismatch`.
6. For `i > 0`: `sequence == i`, `previous_transition_hash == digest(records[i-1])`, `before_state_hash == records[i-1]['after_state_hash']`, `seal_hash == digest(seal)` — else `transition_chain_broken`.
7. `digest(state) == records[-1]['after_state_hash']` — else `loop_projection_stale`. **This is the gate that makes a state-only edit fail before any action.**
8. Every `state['iterations']` entry reconciles against the ledger *and* against the persisted executor-run state without inspecting the mutable live workspace: one `iteration_recorded` record per entry in order with matching `index` / `stage_id` / `run_relative` / `result_hash` / `tree_hash`; `executor_runtime.read(loop_dir/run_relative/'state.json')` must report the same `result_hash`; and acceptance is monotone (`entry['accepted'] is False or run_state['parent_accepted'] is True`), never strict equality — else `iteration_evidence_mismatch`. This deliberately permits the parent to accept the run out of band before `checkpoint()` records that fact, and permits later iterations to change the shared workspace. A live `executor_runtime.inspect()` is reserved for redirect of the current last iteration.
9. Status/type/budget checks: `status in STATUSES`, `0 <= iteration <= envelope['max_iterations']`, finite `deadline`, `(status == 'loop_accepted') == bool(state['loop_approval'])`.

**Crash ordering** (every mutating action, in this exact order):

1. Write the authoritative artifacts first — the iteration run directory (owned entirely by `executor_runtime`), selection/checkpoint receipts, `redirect-receipt.json`, the continuation loop's `loop-seal.json`. All create-only.
2. Write `transitions/NNNN-<kind>.json` create-only. **This is the commit point.**
3. Atomically republish `loop-state.json` with `executor_runtime.save`.

**Recovery** (`bounded_loop.recover(loop_dir)`, exposed as `loop-recover`):

- Runs load steps 1–6 (chain integrity) unconditionally.
- If `digest(state) == records[-1]['after_state_hash']` → run load steps 8–9 against that state before returning `{'status':'projection_current'}`; no write.
- Else if `digest(state) == records[-1]['before_state_hash']` → replay **only** the last record: `candidate = _apply(state, records[-1])`; require `digest(candidate) == records[-1]['after_state_hash']`; run load steps 8–9 against `candidate`; republish → `{'status':'projection_replayed'}`.
- Else → `ContractError('loop_projection_unrecoverable')`.
- `recover` never imports or calls `harness_adapter`, `executor_runtime.advance/initialize`, `loop_menu.choose` or any provider. Tests 27 and 28 assert this with `patch.object`.

### Redirect = immutable continuation, with the workspace pinned across verification

`redirect()` never rewrites the source loop's immutable set. Under `lock(loop/'loop-seal.json')` it re-loads, checks `checkpoint_required` and validates the approval record, then opens a **single bounded section** holding `lock(workspace/'.staged-routing-workspace.json')` — the same marker every `executor_runtime` mutation path takes — so no admitted writer can move the tree between verification and publication. Inside that section, and in this order:

1. `digest(snapshot(workspace)) == approval['carried_tree_hash'] == entry['tree_hash']` — else `redirect_workspace_changed`. This runs before live run inspection so workspace drift has one stable public reason.
2. `executor_runtime.inspect(loop/entry['run_relative'])` must report `parent_accepted is True` and the recorded `result_hash`. That call re-verifies `result['final_tree_hash']` against the same live tree (`executor_runtime.py:172-173`), so **only hash-bound verified progress can carry**. `inspect` takes no lock, so there is no nested acquisition.
3. `validate_envelope(new_envelope, verify_files=True)`; `new_envelope['workspace'] == seal['workspace']` — else `continuation_workspace_mismatch`. The goal and the stage menu may change; that is the point of a redirect.
4. `_initialize_unlocked(new_envelope, registry, new_loop_dir, ..., continuation={...}, expected_initial_tree_hash=carried_tree_hash)` — see below. It re-snapshots immediately before publication and aborts without publishing on any drift.
5. `redirect-receipt.json` in the source via `runtime_support.create_file` — create-only, so a second redirect from the same evidence fails `redirect_already_recorded`.
6. Commit `transitions/NNNN-redirect.json`, then republish the source projection as terminal `redirected` with `redirected_to`.

`_initialize_unlocked` (private; assumes the caller already holds the workspace marker, acquires nothing):

```
observed  = snapshot(root)                                    # (i) pre-snapshot
if expected_initial_tree_hash is not None and digest(observed) != expected: raise 'continuation_tree_drift'
resolve and validate loop_dir and loop_dir.parent with `stage_contracts.no_links`; require the destination not to exist or be a symlink; enforce the same loop/workspace non-overlap check as public initialize; then create loop_dir (create-only; 'loop_dir_exists' otherwise) and write loop.json, registry.json
observed2 = snapshot(root)                                    # (ii) post-snapshot, immediately pre-publication
if digest(observed2) != digest(observed): raise 'continuation_tree_drift'
if expected_initial_tree_hash is not None and digest(observed2) != expected: raise 'continuation_tree_drift'
write loop-seal.json  (initial_tree_hash = digest(observed2)) # ← publication point
write transitions/0000-genesis.json (create-only); publish loop-state.json
```

Before any directory creation, `_initialize_unlocked` canonicalizes the destination, calls `stage_contracts.no_links(loop_dir.parent)`, rejects a pre-existing or symlinked destination, and enforces `loop_inside_workspace` for either-direction overlap with the workspace. On any raise after directory creation and before `loop-seal.json` exists, it removes only the directory **it** created, guarded on the absence of `loop-seal.json` — so an aborted redirect publishes no continuation loop, no receipt and no ledger record.

`bounded_loop.initialize` is the public wrapper: it runs `validate_envelope` unlocked as a cheap fail-fast, then takes `lock(workspace/'.staged-routing-workspace.json')` and calls `_initialize_unlocked`. It never calls any `executor_runtime` mutation path, so the marker is never nested. `redirect` calls `_initialize_unlocked` directly because it already holds the marker.

**Source immutable set** — byte-identical across a redirect:

```
loop.json, registry.json, loop-seal.json,
every transitions/NNNN-*.json that existed before the redirect,
every file under iterations/**,
every *-receipt-*.json that existed before the redirect
```

**Exactly two paths are new** (`redirect-receipt.json`, `transitions/<next>-redirect.json`) and **exactly one path is modified** (`loop-state.json`, the projection). Test 34 asserts this set relation literally.

Invariant worth testing: the continuation's `initial_tree_hash == carried_tree_hash`, so iteration 1 of the continuation starts on exactly the verified tree.

### Route selection inside an iteration is explicit

`executor_runtime.advance` reaches a real worker only with a `fixed_route`, or with a Jev reply admitted through `executor_routing`. With neither, `require_admission(None)` raises and is swallowed (`executor_runtime.py:248-259`), `executor_routes.decide` falls to `provider_unavailable` / `selected='owner'` (`executor_routes.py:129-130`), and the run ends at the control route as `needs_review` (`executor_runtime.py:262-265`). The CLI supplies no parent classifier, so it can never take the Jev path.

Therefore `bounded_loop.advance` accepts `fixed_route=None` and passes it **verbatim** to `executor_runtime.advance`, and `loop-advance` exposes `--fixed-route`. This is the loop's *route* selection (which admitted executor runs the stage) and is orthogonal to `--fixed-stage`, the loop's *stage* selection.

### Typed boundary for later Luna adapter admission — already present, untouched

`assets/executor-routes.json` ships `luna_low`/`luna_medium`/`luna_max` with `status:"unverified"`, `adapter:null`, `model:null`. `executor_routes.available()` admits an executor only when `status=='approved-for-pilot'` **and** `_ready_ok()` matches route/registry/stage/adapter digests, identity, mode and finite expiry; `validate_registry()` additionally demands non-null `model`/`effort`/`approval_sha256`, a non-alias exact model name, `write_benchmark_sha256` for `bounded_write`, and pinned adapter argv. Admitting Luna later is a registry edit plus a reviewed pinned adapter — **no loop code changes**. Slice 1 adds a negative test (24) proving a loop cannot reach a `luna_*` route today.

### Where Jev is allowed to speak

`loop_menu.choose()` reuses purpose **`stage_transition`** (already in `typed_jev.PURPOSES` and `outbound_admission.PURPOSES`) so the outbound boundary is unchanged: `require_admission()` → parent classifier → canonical redactor → exact payload grant. Thresholds mirror `stage_transition.py:51` (`confidence>=.9`, `p>=.9`, `margin>=.2`); anything else, any exception, or a missing admission yields `return_to_parent`. Loop-level `jev_calls`/`jev_usage` accumulate through `jev_delta` in the ledger; per-iteration Jev spend stays inside each run's own budget.

---

## Files

### New

| Path | Purpose |
|---|---|
| `assets/schemas/loop-envelope.schema.json` | Parent-sealed loop contract |
| `assets/schemas/loop-receipt.schema.json` | Selection / checkpoint / redirect receipts |
| `assets/schemas/loop-transition.schema.json` | One hash-chained ledger record |
| `scripts/loop_menu.py` | Closed menu + typed selection (mirrors `stage_transition.py`) |
| `scripts/bounded_loop.py` | Chain controller over `executor_runtime` |
| `scripts/tests/loop_fixtures.py` | 2-stage synthetic envelope + walkthrough generator on `routing_fixtures` |
| `scripts/tests/test_loop_menu.py` | Menu unit tests (1–10) |
| `scripts/tests/test_bounded_loop.py` | Chain / ledger / checkpoint / continuation tests (11–40) |
| `references/bounded-loop.md` | Loop reference doc (Russian, house style) |

### Modified

| Path | Change |
|---|---|
| `scripts/route_cli.py` | Add `loop-*` subcommands incl. `--fixed-route`; extend the exit-code map at `:74`; `candidate_version` `'1.2.0'` → `'1.3.0'` at `:47` |
| `SKILL.md` | `version: 1.2.0` → `1.3.0`; short "Версия 1.3" section; link to `references/bounded-loop.md` |
| `scripts/install_skill.py` | `VERSION = "1.3.0"` (`:16`) |
| `scripts/bounded_runtime.py` | `SKILL_VERSION = "1.3.0"` (`:27`) |
| `scripts/tests/test_route_cli.py` | Three literal pins at `:46-48` → `'1.3.0'` / `'version: 1.3.0'` |
| `scripts/tests/test_skill_contract.py` | `assertIn("version: 1.3.0", text)` (`:19`) |
| `scripts/tests/test_bundle_skill_version.py` | `MANIFEST_VERSION = "1.3.0"` (`:27`) |
| `README.md` | "The installed skill version is **1.3.0**" (`:47`) |
| `references/verification.md` | Append "Slice 1 boundary": what is and is not proven |

Five independent version literals exist — `install_skill.VERSION`, `bounded_runtime.SKILL_VERSION`, the `SKILL.md` frontmatter, `route_cli` `candidate_version`, and the `README.md` sentence — plus three test pins (`test_route_cli.py:46-48`, `test_skill_contract.py:19`, `test_bundle_skill_version.py:27`). All eight move in the same commit or the suite goes red.

`test_skill_contract.test_local_skill_links_exist` requires every non-`https://` link in SKILL.md to resolve to a real file — `references/bounded-loop.md` must land in the same commit as the SKILL.md link. The `description` line must stay ≤60 chars (unchanged).

### Loop directory layout

```
<loop_dir>/
  loop.json                      canonical envelope   (create-only)
  registry.json                  canonical registry   (create-only)
  loop-seal.json                 seal                 (create-only)
  transitions/NNNN-<kind>.json   ledger               (create-only, append-only)
  loop-state.json                projection           (atomic republish only)
  iterations/<n>/run/            executor_runtime run (immutable once written)
  selection-receipt-<n>.json     loop-receipt kind=selection   (create-only)
  checkpoint-receipt-<n>.json    loop-receipt kind=checkpoint  (create-only)
  redirect-receipt.json          loop-receipt kind=redirect    (create-only)
```

`<loop_dir>` must be outside the workspace: `executor_runtime.initialize` refuses `run.is_relative_to(root) or root.is_relative_to(run)` (`executor_runtime.py:87`, `controller_worker_overlap`), and `bounded_loop.initialize` refuses the same at loop level with `loop_inside_workspace`.

---

## Contracts

### `assets/schemas/loop-envelope.schema.json`

Must stay inside the `schema_validation.KEYWORDS` subset; `$ref` is only legal as `#/$defs/<name>`, so stage envelopes **cannot** be referenced across files. `stages` items are declared `{"type":"object"}` and each is validated in Python by `stage_contracts.validate_stage()` — the same split `executor_runtime` already uses.

```jsonc
{
  "type": "object", "additionalProperties": false,
  "required": ["schema_version","loop_id","goal","workspace","max_iterations","max_seconds","parent_owned","stages"],
  "properties": {
    "schema_version": {"const": 1, "type": "integer"},
    "loop_id":   {"type":"string","minLength":1,"maxLength":128},
    "goal":      {"type":"string","minLength":1,"maxLength":2000},
    "workspace": {"type":"string","minLength":1,"maxLength":2048},
    "max_iterations": {"type":"integer","minimum":1,"maximum":4},
    "max_seconds":    {"type":"integer","minimum":1,"maximum":3600},
    "parent_owned":   {"const": true},
    "stages": {"type":"array","minItems":1,"maxItems":8,"uniqueItems":true,
               "items":{"type":"object"}}
  }
}
```

`bounded_loop.validate_envelope()` then enforces, each with its own reason code:

- `duplicate_stage_identity` — `stage_id`s not unique across `stages`
- `reserved_stage_identity` — a `stage_id` equals `return_to_parent` or `stop`
- `stage_workspace_mismatch` — a stage's `workspace` ≠ envelope `workspace`
- `stage_budget_exceeds_loop` — a stage's `limits.max_seconds` > envelope `max_seconds`
- `stage_input_mutated_by_earlier_stage` — for `i < j`, a path in `stages[j].inputs[*].path` appears in `stages[i].allowed_paths`
- plus `stage_contracts.validate_stage(stage, verify_files=…)` per stage, unchanged

### `assets/schemas/loop-transition.schema.json`

`schema_version` const 1; `sequence` integer 0–31; `kind` enum `[genesis,iteration_started,iteration_recorded,interrupted,checkpoint,redirect,accept]`; `previous_transition_hash` / `before_state_hash` sha256-or-null; `seal_hash` / `after_state_hash` sha256; `status` / `reason` strings; `iteration` integer 0–4; `evidence` object with the exact keys above (sha256-or-null, `accepted` boolean); `jev_delta` `{calls,input_tokens,output_tokens}` non-negative integers; `recorded_at` number; `parent_acceptance` **const false**.

### `assets/schemas/loop-receipt.schema.json`

`schema_version` const 1; `kind` enum `[selection,checkpoint,redirect]`; `loop_hash`, `loop_state_hash`, `transition_hash` sha256; `iteration` 0–4; `allowed` array 1–16 of strings; `decision` string; `source` enum `[parent,jev,policy]`; `reason` string; `answers` object; `confidence` `{choice,margin}` numbers; `evidence_hash`, `carried_tree_hash` sha256-or-null; `usage` `{input_tokens,output_tokens}`; `provider_model` string-or-null; `parent_acceptance` **const false**.

### `loop-seal.json` / `loop-state.json`

```python
seal  = {'schema_version':1,'loop_hash':digest(envelope),'registry_hash':digest(registry),
         'workspace':str,'initial_tree_hash':digest(snapshot(workspace)),
         'evidence_mode':'synthetic'|'live','gate_evidence':…,
         'continuation': None | {'schema_version':1,'source_loop_hash','source_loop_state_hash',
                                 'source_transition_hash','source_iteration','carried_result_hash',
                                 'carried_tree_hash','reviewer','approved_by':'parent'}}

state = {'schema_version':1,'seal_hash':digest(seal),'status','reason','iteration',
         'created_at','deadline','iterations':[{'index','stage_id','run_relative',
              'result_hash','tree_hash','status','accepted'}],
         'jev_calls','jev_usage','last_selection','last_checkpoint','redirected_to','loop_approval'}
```

`loop.json` and `registry.json` hold the canonical envelope and registry so `_load()` can re-derive `seal['loop_hash']` and `seal['registry_hash']` from bytes on disk; any mismatch is `sealed_loop_changed`.

### Approval records (exact key sets, like `executor_runtime.accept`)

```python
continue: {'decision':'continue','evidence_hash','reviewer','evidence_kind'}
redirect: {'decision':'redirect','evidence_hash','carried_tree_hash','reviewer','evidence_kind'}
accept:   {'decision':'accept','loop_evidence_hash','reviewer','evidence_kind'}   # digest(state['iterations'])
```

---

## Symbols

```python
# scripts/loop_menu.py
FIELDS = {'goal_hash','iteration','max_iterations','last_status','last_accepted',
          'scope_violations','remaining_stage_ids','hard_owner_boundary'}
CONTROLS = ('return_to_parent','stop')

def allowed(ctx) -> list[str]:
    _shape(ctx)                                     # exact FIELDS set; types; ranges
    if ctx['hard_owner_boundary'] or ctx['scope_violations']: return list(CONTROLS)
    if ctx['iteration'] > 0 and not ctx['last_accepted']:     return list(CONTROLS)
    if ctx['iteration'] >= ctx['max_iterations'] or not ctx['remaining_stage_ids']:
        return list(CONTROLS)
    return list(ctx['remaining_stage_ids']) + list(CONTROLS)

def choose(ctx, *, proposed=None, jev=None, admission=None,
           classification='synthetic', documents=(), source_guard=None) -> dict
```

```python
# scripts/bounded_loop.py
TERMINAL = {'redirected','stopped','loop_accepted'}
STATUSES = TERMINAL | {'ready','iteration_running','checkpoint_required'}
KINDS    = ('genesis','iteration_started','iteration_recorded','interrupted','checkpoint','redirect','accept')
MAX_TRANSITIONS = 32

def validate_envelope(envelope, *, verify_files=True) -> None
def initialize(envelope, registry, loop_dir, *, evidence_mode, gate_evidence=None,
               continuation=None, expected_initial_tree_hash=None) -> dict   # takes the workspace marker
def _initialize_unlocked(envelope, registry, loop_dir, *, evidence_mode, gate_evidence,
                         continuation, expected_initial_tree_hash) -> dict   # acquires nothing
def _apply(state, record) -> dict                                            # pure, total
def _commit(loop, state, kind, **evidence) -> dict                           # create-only record, then republish
def inspect(loop_dir) -> dict
def recover(loop_dir) -> dict
def advance(loop_dir, *, fixed_stage=None, fixed_route=None, jev=None,
            providers=None, admissions=None) -> dict          # fixed_route passed verbatim to executor_runtime.advance
def checkpoint(loop_dir, decision, *, approval=None) -> dict   # 'continue' | 'stop'
def redirect(loop_dir, new_loop_dir, envelope, registry, *,
             approval, evidence_mode, gate_evidence=None) -> dict
def accept(loop_dir, approval) -> dict
```

Reused verbatim, no signature changes: `executor_runtime.{initialize,advance,inspect,accept,transition,snapshot,lock,save,read}`, `runtime_support.{create_file,save_json}`, `stage_contracts.{validate_stage,workspace,no_links,contract,schema,file_hash,read_file}`, `schema_validation.{loads,canonical,digest,validate,ContractError}`, `executor_routes.{load_registry,validate_registry,hard_owner,available}`, `outbound_admission.require_admission`, `typed_jev.{TypedJev,JevError,validate_reply,PURPOSES}`.

---

## CLI surface (`scripts/route_cli.py`)

Same `Parser`/`_json`/`_providers` helpers and the same blanket `except` that prints `{"status":"blocked","reason":"invalid_or_unavailable_staged_contract"}`.

```
loop-validate   --envelope --registry --registry-sha256
loop-init       --envelope --registry --registry-sha256 --loop --evidence-mode synthetic|live [--gate-evidence]
loop-advance    --loop [--fixed-stage ID] [--fixed-route ID] [--allow-typesafe --grants FILE]
loop-checkpoint --loop --decision continue|stop [--approval FILE]
loop-redirect   --loop --new-loop --envelope --registry --registry-sha256 --approval --evidence-mode [--gate-evidence]
loop-inspect    --loop
loop-recover    --loop
loop-accept     --loop --approval
```

Exit codes: extend `route_cli.py:74` to `return 20 if result.get('status') in ('needs_review','stopped','checkpoint_required') else 0`. `redirected`, `loop_accepted`, `projection_current` and `projection_replayed` are completed parent actions → 0. `2` stays the blocked-contract code.

As with `route-advance`, the CLI has **no auto-grant path**: `--allow-typesafe` alone still fails closed because `OutboundAdmission` requires a parent classifier the CLI does not provide. `--fixed-route` is therefore the only way the CLI reaches a real executor, exactly as `route-advance --fixed-route` already is. The Jev-*selection* proof lives at the Python API level with `outbound_fixtures.admission`, as `test_executor_runtime.py:46-57` does for routing.

---

## TDD sub-slices — strict RED first

Every test module opens with the existing guard idiom (`test_stage_transition.py:5-9`), so the first run is RED with a legible message rather than a collection error:

```python
SCRIPTS = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(SCRIPTS))
try: import loop_menu as M
except ImportError: M = None
...
def setUp(self): self.assertIsNotNone(M, 'bounded loop menu missing')
```

| Sub-slice | Write tests | Then implement |
|---|---|---|
| **1a** Closed menu | `test_loop_menu.py` (1–10) | `scripts/loop_menu.py` |
| **1b** Envelope, seal, ledger genesis | `test_bounded_loop.py` (11–18) | `loop-envelope.schema.json`, `loop-transition.schema.json`, `validate_envelope`, `_apply`, `_commit`, `_initialize_unlocked`, `initialize`, `_load`, `inspect` |
| **1c** One iteration + ledger integrity | `test_bounded_loop.py` (19–29) | `advance` (with `fixed_route`), `recover` |
| **1d** Checkpoint, continuation, CLI, docs | `test_bounded_loop.py` (30–40) | `checkpoint`, `redirect`, `accept`, `loop-receipt.schema.json`, CLI, docs, version bump |

### RED / GREEN commands

```bash
cd /home/ivan/worktrees/hermes-bounded-loop-v1

# RED for one sub-slice (expect failures naming the missing module/symbol)
python3 -B -m unittest discover -s scripts/tests -p test_loop_menu.py -v
python3 -B -m unittest discover -s scripts/tests -p test_bounded_loop.py -v

# GREEN for that sub-slice, then the whole suite must stay green
python3 -B -m unittest discover -s scripts/tests -v
```

---

## Test inventory (negative cases first)

### `scripts/tests/test_loop_menu.py`

1. `test_menu_is_closed` — `allowed()` == remaining stage ids + `['return_to_parent','stop']`, nothing else.
2. `test_unknown_field_rejected` — `ctx['permissions']=['all']` → `ContractError`.
3. `test_microrouting_rejected` — `proposed` in `('read','edit','run_tests','deploy','next_stage:anything')` → `ContractError`.
4. `test_scope_violation_returns_parent` — non-empty `scope_violations` → controls only.
5. `test_unaccepted_previous_iteration_blocks_continue` — `last_accepted=False` → controls only.
6. `test_budget_exhausted_blocks_continue` — `iteration == max_iterations` → controls only.
7. `test_hard_owner_boundary_blocks_continue`.
8. `test_low_confidence_returns_parent` — stub provider at `confidence=.1` → `return_to_parent`.
9. `test_missing_provider_returns_parent` — `jev=None` → `return_to_parent`, `source == 'policy'`.
10. `test_no_admission_no_send` — provider present, `admission=None` → provider never called, `return_to_parent`.

### `scripts/tests/test_bounded_loop.py`

**1b — envelope, seal, ledger genesis (11–18)**

11. `test_envelope_duplicate_stage_id_refused` → `duplicate_stage_identity`
12. `test_reserved_stage_identity_refused` — a stage named `stop` → `reserved_stage_identity`
13. `test_stage_workspace_mismatch_refused` → `stage_workspace_mismatch`
14. `test_stage_budget_exceeds_loop_refused` → `stage_budget_exceeds_loop`
15. `test_future_stage_input_in_prior_allowed_paths_refused` — re-point stage B's input at `sample.py` (stage A's `allowed_paths`) → `stage_input_mutated_by_earlier_stage`; the unmodified fixture envelope validates
16. `test_loop_dir_create_only` — second `initialize` → `ContractError('loop_dir_exists')`
17. `test_loop_destination_boundaries` — a loop inside/around the workspace → `loop_inside_workspace`; a symlinked parent or symlink destination → existing `unsafe_symlink`/closed-path error before creation; no artifact appears at the symlink target
18. `test_genesis_layout_and_projection` — `loop.json`, `registry.json`, `loop-seal.json`, `transitions/0000-genesis.json` exist; `digest(loop.json) == seal['loop_hash']`; `digest(registry.json) == seal['registry_hash']`; genesis has `previous_transition_hash is None`, `before_state_hash is None`, `seal_hash == digest(seal)`; `digest(loop-state.json) == genesis['after_state_hash']`; `parent_acceptance is False`

**1c — one bounded iteration and ledger integrity (19–29)**

19. `test_first_iteration_runs_pinned_adapter_and_checks` — `advance(fixed_route='fixture_worker')` → `checkpoint_required` / `iteration_ready_for_parent_review`, `sample.py` contains `return a + b`, run `attempts == 1`, ledger gains `iteration_started` then `iteration_recorded`
20. `test_worker_unreachable_without_fixed_route_or_admission` — same advance with `fixed_route=None` and no `executor_routing` admission → iteration ends at the `owner` control route, `patch.object(harness_adapter,'execute')` not called (regression for `executor_routes.py:129-130`)
21. `test_deterministic_failure_precedes_semantic` — `bad_result` adapter + `features.semantic_cascade` + counting `Judge` → `judge.calls == 0`, iteration semantic status `blocked_deterministic`
22. `test_one_bounded_correction_inside_iteration` — `retry_adapter` + `features.stage_transition`; drive `executor_runtime.transition` on the iteration's own run → `attempts == 2`; a further `bounded_loop.advance` runs no worker
23. `test_scope_violation_returns_to_parent_not_next_stage` — `scope_violation` adapter → `checkpoint_required`, and `loop_menu.allowed()` at that checkpoint is controls only
24. `test_luna_routes_remain_unavailable_in_loop` — a stage with `executor_candidates=['luna_medium','owner']` → `executor_routes.available(...) == ['owner']`, iteration ends at the control route, adapter never executed
25. `test_workspace_lock_not_held_across_iteration` — regression for the nested-`flock` trap; fails with `one_writer_lock_busy` if `bounded_loop.advance` ever locks the workspace marker
26. `test_interrupted_iteration_no_implicit_retry` — `patch.object(executor_runtime,'initialize',side_effect=RuntimeError)` so `advance` commits `iteration_started` and then crashes; a fresh `advance` yields `checkpoint_required` / `interrupted_iteration_no_implicit_retry` with an `interrupted` record appended, and `patch.object(harness_adapter,'execute')` asserts not called. No file is hand-edited.
27. `test_crash_between_record_and_projection_recovers` — commit a record, then restore the *previous* projection bytes (the exact crash window of step 2→3); `inspect` raises `loop_projection_stale`; `recover()` returns `projection_replayed` and restores `digest(state) == records[-1]['after_state_hash']`; on a current projection, tamper the referenced run state and assert `recover()` raises `iteration_evidence_mismatch` instead of returning `projection_current`; `harness_adapter.execute`, `executor_runtime.advance` and `loop_menu.choose` are all patched and asserted not called
28. `test_state_only_edit_refused_before_action` — hand-edit `loop-state.json` (flip `iterations[0]['accepted']`) → `inspect` and `advance` both raise `loop_projection_stale` before any action, `recover()` raises `loop_projection_unrecoverable`, adapter not called
29. `test_ledger_and_seal_tamper_refused` — edit a transition record → `transition_chain_broken`; delete the last record → `transition_ledger_gap`; add an out-of-sequence file → `transition_ledger_gap`; edit `loop.json` or `registry.json` → `sealed_loop_changed`

**1d — checkpoint, continuation, acceptance (30–40)**

30. `test_continue_requires_accepted_iteration` — `checkpoint('continue')` without `executor_runtime.accept` → `continue_requires_accepted_evidence`
31. `test_budget_blocks_third_iteration` — `max_iterations=2`; iteration 1 (stage A, `fixed_route='fixture_worker'`, accepted) → continue → iteration 2 (stage B, `fixed_route='deterministic'`, accepted) → continue → `advance` raises `loop_iteration_budget`, and the menu at that point is controls only
32. `test_redirect_requires_accepted_evidence` → `redirect_requires_accepted_evidence`
33. `test_redirect_requires_unchanged_workspace` — accept, mutate `sample.py`, redirect → `redirect_workspace_changed`
34. `test_redirect_creates_immutable_continuation` — hash every file under the source loop before and after; assert the immutable set (`loop.json`, `registry.json`, `loop-seal.json`, pre-existing `transitions/*`, all of `iterations/**`, pre-existing `*-receipt-*.json`) is byte-identical; assert new paths are **exactly** `{redirect-receipt.json, transitions/<next>-redirect.json}`; assert modified paths are **exactly** `{loop-state.json}`; assert `continuation.carried_result_hash == source result_hash` and the continuation's `initial_tree_hash == carried_tree_hash`
35. `test_redirect_is_single_use` → `redirect_already_recorded`
36. `test_redirect_rejects_foreign_workspace` → `continuation_workspace_mismatch`
37. `test_redirect_aborts_without_publishing_on_tree_drift` — patch `bounded_loop.snapshot` so the post-snapshot (ii) differs from the pre-snapshot (i) → `continuation_tree_drift`; assert `new_loop_dir` does not exist, `redirect-receipt.json` does not exist, no `redirect` record was appended, and the source status is still `checkpoint_required`
38. `test_redirect_does_not_nest_workspace_lock` — one source loop proves a successful redirect completes. A separate independently initialized source loop is left at `checkpoint_required`; with an external `flock` held on its `.staged-routing-workspace.json`, redirect raises `one_writer_lock_busy` before publication (proving it takes the marker exactly once, while `executor_runtime.inspect` under it does not re-acquire)
39. `test_loop_accept_is_parent_only_and_binds_evidence` — wrong `loop_evidence_hash` → `ContractError`; correct → `loop_accepted`; every receipt and every ledger record reports `parent_acceptance is False`
40. `test_iteration_evidence_mismatch_refused` — repoint an `iterations[*].run_relative` at a different persisted run state or alter that run state's `result_hash` → `iteration_evidence_mismatch`; separately prove out-of-band parent acceptance while the loop entry remains `accepted=False` is allowed by the monotone rule and reaches `checkpoint()`

### `scripts/tests/loop_fixtures.py`

Builds on `routing_fixtures` (`routing_fixtures.py:9` `stage`, `:33` `registry`, `:81` `attach_adapter`). Synthetic only; no credentials, no live claims.

- `envelope(root)` → a 2-stage envelope over one workspace:
  - **Stage A** = `F.stage(root)` — `role='implement'`, `mode='bounded_write'`, `allowed_paths=['sample.py']`, `executor_candidates=['fixture_worker','owner']`, driven with `fixed_route='fixture_worker'` and the pinned adapter from `F.attach_adapter`.
  - **Stage B** — `role='transform'`, `mode='bounded_write'`, `executor_candidates=['deterministic','owner']`, `inputs=[{'id':'spec','path':'spec.md',…}]` (**stable**: `spec.md` is in no earlier stage's `allowed_paths`), `allowed_paths=['notes.md']`, `output_contract.required_paths=['notes.md']`, `max_changed_files=1`, plus a pinned `deterministic_action` writing `notes.md` and a pinned check reading it — the path already proven by `test_routing_boundary_closure.py:31`. Driven with `fixed_route='deterministic'`. No new adapter and no new registry route are needed.
  - The fixture writes `spec.md` into the workspace and keeps `action.py` / `check_b.py` **outside** the workspace, because `stage_contracts.validate_pins(command, worker_root)` rejects pins inside the worker root.
- `registry()` — `routing_fixtures.registry()` (base registry + `fixture_worker`) with `attach_adapter` applied.
- `envelope_2(root)` — the continuation envelope written eagerly by `walkthrough`; its stages pin only stable workspace inputs such as `spec.md` that no stage of envelope 1 may write, so it remains valid after iteration 1; same `workspace`, different `goal` and stage menu.
- `Judge` — a counting provider for test 21.
- `walkthrough(root)` — writes a persistent fixture tree and returns the paths used by the Verification walkthrough: `<root>/work/` (workspace), `<root>/loop-envelope.json`, `<root>/loop-envelope-2.json`, `<root>/loop-registry.json` (the fixture registry, containing `fixture_worker` **and** its pinned adapter). Nothing under `root` is a temp directory that disappears between commands.

---

## Tasks

### Sub-slice 1a — closed menu

- [ ] Write `scripts/tests/test_loop_menu.py` with tests 1–10 and the `try: import loop_menu / except ImportError: M = None` guard.
- [ ] Run RED: `python3 -B -m unittest discover -s scripts/tests -p test_loop_menu.py -v` — confirm failures name the missing module.
- [ ] Implement `scripts/loop_menu.py` (`FIELDS`, `CONTROLS`, `_shape`, `allowed`, `choose`) mirroring `stage_transition.py` structure and thresholds; reuse purpose `stage_transition`.
- [ ] Run GREEN: same command, 10/10 pass.

### Sub-slice 1b — envelope contract, seal, ledger genesis

- [ ] Write `scripts/tests/loop_fixtures.py`: stage A, stage B with the **stable** `spec.md` input and disjoint `notes.md` write scope, `registry()`, `envelope()`, `envelope_2()`, `Judge`, `walkthrough()`.
- [ ] Write `scripts/tests/test_bounded_loop.py` tests 11–18.
- [ ] Run RED: `python3 -B -m unittest discover -s scripts/tests -p test_bounded_loop.py -v`.
- [ ] Add `assets/schemas/loop-envelope.schema.json` and `assets/schemas/loop-transition.schema.json` within the `schema_validation.KEYWORDS` subset (no cross-file `$ref`).
- [ ] Implement `bounded_loop.validate_envelope` including `stage_input_mutated_by_earlier_stage`.
- [ ] Implement `_apply` (pure/total), `_commit` (create-only record → atomic projection), `_initialize_unlocked` (pre/post snapshot, `expected_initial_tree_hash`, abort-cleanup), `initialize` (workspace-marker wrapper), `_load` (steps 1–9), `_public`, `inspect`.
- [ ] Persist `loop.json` and `registry.json` canonically and bind them in `_load` step 2.
- [ ] Run GREEN.

### Sub-slice 1c — one bounded iteration, ledger integrity, recovery

- [ ] Write `test_bounded_loop.py` tests 19–29.
- [ ] Run RED.
- [ ] Implement `bounded_loop.advance(loop_dir, *, fixed_stage=None, fixed_route=None, jev=None, providers=None, admissions=None)`: select via `loop_menu.choose` (or `fixed_stage`), write the selection receipt, commit `iteration_started` write-ahead, call `executor_runtime.initialize` then `executor_runtime.advance(run, fixed_route=fixed_route, jev=jev, providers=providers, admissions=admissions)` — `fixed_route` passed **verbatim** — commit `iteration_recorded`, land in `checkpoint_required`.
- [ ] Implement the interrupted path: entering on `iteration_running` commits an `interrupted` record and yields `checkpoint_required` / `interrupted_iteration_no_implicit_retry`; never auto-resume.
- [ ] Implement `bounded_loop.recover` (chain check → current / replay-last-record-only / `loop_projection_unrecoverable`), importing no adapter or provider module.
- [ ] Verify `bounded_loop.advance` holds **only** the `loop-seal.json` lock (test 25 is the guard).
- [ ] Run GREEN.

### Sub-slice 1d — checkpoint, continuation, CLI, docs

- [ ] Write `test_bounded_loop.py` tests 30–40.
- [ ] Run RED.
- [ ] Add `assets/schemas/loop-receipt.schema.json` (`parent_acceptance` const `false`).
- [ ] Implement `bounded_loop.checkpoint` and `accept` (loop-seal lock only).
- [ ] Implement `bounded_loop.redirect`: loop-seal lock → workspace-marker section containing `executor_runtime.inspect`, tree comparison, `validate_envelope`, `_initialize_unlocked(..., expected_initial_tree_hash=carried_tree_hash)`, create-only `redirect-receipt.json`, `redirect` record, projection republish — in that order, aborting before any publication on drift.
- [ ] Add `loop-*` subcommands to `scripts/route_cli.py` including `--fixed-route` on `loop-advance` and `loop-recover`; extend the exit-code map at `route_cli.py:74` with `checkpoint_required` → 20; set `candidate_version` to `'1.3.0'` at `route_cli.py:47`.
- [ ] Write `references/bounded-loop.md`.
- [ ] Bump `SKILL.md` to `version: 1.3.0`, add the "Версия 1.3" section and the link to `references/bounded-loop.md`; keep `description` ≤60 chars and the literals `ready_for_parent_review`, `не является песочницей`, `delegation.model`.
- [ ] Set `install_skill.VERSION = "1.3.0"` (`:16`) and `bounded_runtime.SKILL_VERSION = "1.3.0"` (`:27`).
- [ ] Update the test pins: `test_route_cli.py:46-48` (all three), `test_skill_contract.py:19`, `test_bundle_skill_version.py:27`.
- [ ] Update `README.md:47` to `**1.3.0**`.
- [ ] Append the Slice 1 boundary section to `references/verification.md`.
- [ ] Run GREEN, then the full suite.

### Final verification

- [ ] Full suite green, zero skips: `python3 -B -m unittest discover -s scripts/tests -v`.
- [ ] Local diagnostics run clean: `stage_cli.py doctor`, `route_cli.py route-doctor` (reports `candidate_version` `1.3.0`).
- [ ] `loop-validate` accepts the fixture-written envelope against the fixture-written registry.
- [ ] Synthetic end-to-end walkthrough reaches `redirected` with a byte-identical source immutable set.
- [ ] Live Jev selection exercised separately with a parent-supplied admission and grant.
- [ ] Report results to the parent; do **not** self-accept.

---

## Verification

```bash
cd /home/ivan/worktrees/hermes-bounded-loop-v1

# 1. Full suite — existing + new, expect zero failures and zero skips
python3 -B -m unittest discover -s scripts/tests -v

# 2. Local diagnostics (not a compatibility or API proof)
python3 -B scripts/stage_cli.py doctor
python3 -B scripts/route_cli.py route-doctor

# 3. Generate the persistent fixture tree: workspace, both envelopes, and a registry
#    that actually contains fixture_worker plus its pinned adapter. The shipped
#    assets/executor-routes.json does NOT contain fixture_worker, and
#    executor_routes.available() raises unknown_route on an unknown candidate.
rm -rf /tmp/loop-fixture /tmp/loop /tmp/loop-2
python3 -B -c "
import sys, pathlib
sys.path[:0] = ['scripts', 'scripts/tests']
import loop_fixtures as L
print(L.walkthrough(pathlib.Path('/tmp/loop-fixture')))
"
ENVELOPE=/tmp/loop-fixture/loop-envelope.json
ENVELOPE2=/tmp/loop-fixture/loop-envelope-2.json
REGISTRY=/tmp/loop-fixture/loop-registry.json
SHA="$(sha256sum "$REGISTRY" | cut -d' ' -f1)"

# 4. CLI contract path on the fixture envelope
python3 -B scripts/route_cli.py loop-validate \
  --envelope "$ENVELOPE" --registry "$REGISTRY" --registry-sha256 "$SHA"

# 5. End-to-end synthetic walkthrough (exit codes: 20 at checkpoints, 0 on parent actions)
python3 -B scripts/route_cli.py loop-init --envelope "$ENVELOPE" \
  --registry "$REGISTRY" --registry-sha256 "$SHA" \
  --loop /tmp/loop --evidence-mode synthetic

# --fixed-route is required: without it the iteration ends at the owner control route.
python3 -B scripts/route_cli.py loop-advance --loop /tmp/loop \
  --fixed-stage synthetic-stage --fixed-route fixture_worker        # exit 20

# Build the run approval only AFTER inspect reports the real result_hash.
python3 -B scripts/route_cli.py route-inspect --run /tmp/loop/iterations/1/run > /tmp/iter1.json
python3 -B -c "
import json
r = json.load(open('/tmp/iter1.json'))
json.dump({'decision':'accept','evidence_hash':r['result_hash'],
           'reviewer':'synthetic-parent','evidence_kind':'synthetic'}, open('/tmp/approve.json','w'))
"
python3 -B scripts/route_cli.py route-accept --run /tmp/loop/iterations/1/run --approval /tmp/approve.json

# Build the redirect approval from the same verified evidence plus the live tree hash.
python3 -B -c "
import json, pathlib, sys
sys.path.insert(0, 'scripts')
import executor_runtime as E
from schema_validation import digest
r = json.load(open('/tmp/iter1.json'))
tree = digest(E.snapshot(json.load(open('/tmp/loop/loop-seal.json'))['workspace']))
json.dump({'decision':'redirect','evidence_hash':r['result_hash'],'carried_tree_hash':tree,
           'reviewer':'synthetic-parent','evidence_kind':'synthetic'}, open('/tmp/redirect.json','w'))
"
python3 -B scripts/route_cli.py loop-redirect --loop /tmp/loop --new-loop /tmp/loop-2 \
  --envelope "$ENVELOPE2" --registry "$REGISTRY" --registry-sha256 "$SHA" \
  --approval /tmp/redirect.json --evidence-mode synthetic          # exit 0

python3 -B scripts/route_cli.py loop-inspect --loop /tmp/loop-2     # exit 0, status ready
python3 -B scripts/route_cli.py loop-recover --loop /tmp/loop       # projection_current, exit 0
```

Manual assertions for step 5:

- `/tmp/loop/loop-state.json` reaches `redirected`, and `digest(loop-state.json)` equals the `after_state_hash` of the last record in `/tmp/loop/transitions/`.
- The source immutable set — `loop.json`, `registry.json`, `loop-seal.json`, every pre-existing `transitions/*.json`, all of `iterations/**`, every pre-existing `*-receipt-*.json` — is byte-identical before and after the redirect. The only new paths are `redirect-receipt.json` and `transitions/<next>-redirect.json`; the only modified path is `loop-state.json`.
- `/tmp/loop-2/loop-seal.json` carries `continuation.carried_tree_hash` equal to its own `initial_tree_hash`, and `/tmp/loop-2/transitions/0000-genesis.json` binds `seal_hash == digest(loop-seal.json)`.

**Live Jev, separately and never in the same run as the adapter proof.** With `TYPESAFE_API_KEY` exported locally, drive `loop_menu.choose()` from Python with a parent-supplied `OutboundAdmission` (real classifier, real `agent.redact.redact_sensitive_text`) plus an exact grant — purpose `stage_transition`, classification `synthetic`, expiry ≤1h. Record only the receipt (`decision`, `source`, `confidence`, `usage`), never the payload.

**What this does not prove:** no live executor harness, no Luna admission, no production activation, no calibrated Jev thresholds, no acceptance of any artifact. The transition ledger proves *what the controller committed and in what order*; it is not evidence about the quality of any result, and it does not survive an attacker with write access to both the ledger and the seal. `checkpoint_required` means the parent must look; `loop_accepted` means a human reviewer bound an evidence hash — neither is a statement about result quality. Append exactly this boundary to `references/verification.md`.
