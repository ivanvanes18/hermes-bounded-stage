# Executor routing 1.2.0-candidate.2

## Отдельный протокол

`route-*` — новая staged-ветка, не новая интерпретация старых карточек. `assets/schemas/stage-envelope.schema.json` описывает полный stage. Версия envelope 1 не является legacy plan schema v1: допуск выполняется отдельным validator. Старые init/advance/submit/inspect и планы v1/v2 не меняются.

Родитель фиксирует domain skill name/version/source hash, единственный process owner/name/version/stage, точные исходники, scope, checks, risk и budgets. Компактные domain rules должны быть точными выдержками из связанных source bytes. Доверенные check scripts и adapter code находятся вне worker workspace; argv и executable/script pins закреплены. Shell и свободные model-generated команды запрещены.

Новый workspace содержит marker `.staged-routing-workspace.json`:

```json
{"purpose":"hermes-routing-staged","schema_version":1}
```

Marker сообщает назначение каталога; не является sandbox или разрешением на production. Control run создаётся отдельно от worker tree и никогда не перезаписывается.

## CLI

```sh
python3.13 -B scripts/stage_cli.py route-doctor
python3.13 -B scripts/route_cli.py --help
```

`route-validate`, `route-init`, `route-request` принимают `--stage`, `--registry`, `--registry-sha256`. Последний аргумент — SHA-256 **точных байтов файла registry**, а не canonical JSON. `route-init` дополнительно требует `--run` и `--evidence-mode synthetic|live`; `--gate-evidence` нужен для разрешённых live extension features. В receipts применяется canonical-object digest: это отдельный hash domain.

`route-request` делает только read-only readiness probes и готовит точный запрос, не отправляя его. `route-advance --run ... --fixed-route ...` выбирает уже допущенный родителем route без Jev. Без fixed route нужен отдельно разрешённый provider; отсутствие/ошибка/низкая confidence возвращают owner. Для внешнего Jev нужны одновременно `--allow-typesafe` и `--grants <file>`.

`route-inspect --run ...` проверяет evidence. `route-accept --run ... --approval <file>` требует точной связи approval с evidence hash и evidence kind. Его вызывает родитель из защищённого control boundary, никогда worker. Формат approval: decision=accept, evidence_hash, reviewer, evidence_kind. Сам факт наличия JSON не аутентифицирует пользователя.

`route-transition --run ... --proposed one_bounded_correction --parent-authorized-correction --triage-category implementation_defect` лишь подготавливает одну разрешённую следующую стадию; запуск — отдельный advance. Permission/scope/checks не расширяются. Текущий baseline и предыдущий result hash входят в packet.execution. Второй correction cycle и повтор transition на той же evidence блокируются. `complete` оставляет parent acceptance=false.

Exit codes: 0 — выполненная локальная операция/готовность к parent review; 20 — needs_review/stopped; 2 — blocked contract/environment. Exit 0 не означает client/live readiness.

## Права и failure policy

High risk или production/secrets/irreversible/money/publication/architecture_change направляют owner. Private-контекст не допускается к внешнему executor. Public-redacted требует external_allowed; сам этот flag должен быть подтверждён родителем. Все реальные routes по умолчанию unverified. Deterministic route требует отдельно закреплённого deterministic_action; без него не ready.

Readiness связан с stage/registry/route/adapter digests, точной identity, mode и finite expiry. Runtime proof сверяется повторно: выбранная модель не считается фактической моделью без adapter evidence. Документированный [adapter protocol](adapter-protocol.md) требует независимого аудита реализации реального harness.

До запуска worker списывается попытка; interrupted worker не перезапускается молча. Межпроцессные locks дают одного writer. После запуска контроллер самостоятельно вычисляет diff относительно frozen baseline, проверяет allowed/required paths, запускает pinned checks и повторно проверяет source drift. Изменение checks, methodology или context не публикуется как успешный результат.

## Контекст и semantic gates

Reranking загружает только реальные hash-bound кандидаты, проверяет точные line ranges, выполняет bounded batches Noul, сохраняет весь shortlist/scores и делает deterministic top-k union mandatory. Оценки между 0.1 и 0.9, отсутствие provider или budget расширяют выбор до полного допустимого shortlist; превышение общего бюджета блокирует, а не обрезает mandatory evidence. Источники перепроверяются после model call и перед публикацией/приёмкой.

Semantic cascade вызывается после deterministic success. True означает проблему; любое значение >=0.10 даёт needs_review. Неполный/невалидный ответ, отсутствующий источник или слишком длинное evidence не становятся semantic pass. Короткие excerpts в exception нужны для локализации; судье отправляется целый **ограниченный** evidence item, а не скрыто обрезанный документ. Поддержка бинарных источников в semantic mode не заявлена: не-UTF-8 приводит к needs_review.

Глобальный max_jev_calls учитывает routing, rerank, cascade, triage и transition. Для каждой внешней цели нужен отдельный matching content grant. Grant включает schema_version=1, approved_by=parent, purpose, classification=synthetic/public-redacted, payload_sha256 и конечный expires_at не дальше одного часа. Grant не заменяет защищённую parent boundary и не выдаёт прав worker.


## Candidate.2: context/semantic outbound admission

`advance(..., admissions={purpose: OutboundAdmission(...)})` receives trusted
parent-only policy objects separately from providers and the worker packet.
Supported purposes here are `context_reranking` and `semantic_cascade`.
The classifier must be the parent's existing reviewed data-policy adapter;
there is deliberately no default classifier. The production redactor always
calls `agent.redact.redact_sensitive_text(force=True,
redact_url_credentials=True)`; a missing/failed canonical redactor denies egress.
Test fixtures patch that adapter only in tests and do not establish live privacy.

Classification covers full local context documents, not only the selected lines,
and all objective/source/output/question data. Raw private/contract/act/secret
or unknown classifications are denied; redaction never upgrades classification.
Only synthetic or independently public-redacted input may be admitted, and only
with exact parent grants matching the redacted canonical wire payload, purpose,
classification and expiry (at most one hour). All batch grants are preflighted
before the first call; source identity and grants are checked again at send time.
A source change detected before first dispatch means zero provider calls. If a
source changes during an already-issued provider call, the prior call cannot be
undone; subsequent dispatch/result publication is blocked by freshness checks.

Context receipts contain candidate IDs, source/fragment hashes, selection,
scores and outbound hashes, not source fragments or objectives. Local worker
context is separately re-materialized from verified sources; worker input is not
an outbound receipt. Semantic receipts keep flag IDs, source/output pointers,
hashes and probabilities, not raw excerpts. CLI has no auto-grant path: without
the trusted parent integration, evidence-bearing features fail closed.
