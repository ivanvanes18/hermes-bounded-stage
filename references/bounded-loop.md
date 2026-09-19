# Bounded execution loop 1.3.0-candidate.1

## Что добавляет слой loop

`route-*` ограничивает **один** stage. `loop-*` связывает несколько таких stage в цепочку, скреплённую хешами, с обязательной остановкой у родителя между итерациями. Это не планировщик и не второй runtime: всё внутри итерации целиком делегируется существующему `executor_runtime` без изменений в нём.

Родитель опечатывает **loop envelope**: цель, workspace, бюджеты и закрытый упорядоченный список из 1–8 уже допущенных им stage envelopes. Jev не сочиняет stage. На каждом checkpoint она выбирает один пункт закрытого меню: `<stage_id>` оставшегося допущенного stage, `return_to_parent` или `stop`. Дисциплина та же, что у `executor_routes.request()` и `stage_transition.choose()`; используется существующий purpose `stage_transition`, новый outbound boundary не появляется.

Одна итерация — ровно один запуск `executor_runtime`. `stage-envelope.schema.json` ограничивает `limits.max_worker_calls` двумя, а `worker-packet.schema.json` — `execution.attempt` двумя, поэтому в итерации помещается не более одной попытки и одной разрешённой родителем correction. Loop наращивает число итераций, но не число correction, и каждая итерация — **другой** заранее допущенный stage.

## Envelope

```json
{"schema_version":1,"loop_id":"...","goal":"...","workspace":"/abs/path",
 "max_iterations":2,"max_seconds":600,"parent_owned":true,"stages":[{"...": "stage envelope"}]}
```

`bounded_loop.validate_envelope()` отказывает с отдельным кодом причины: `duplicate_stage_identity`, `reserved_stage_identity` (`stage_id` равен `return_to_parent` или `stop`), `stage_workspace_mismatch`, `stage_budget_exceeds_loop`, `stage_input_mutated_by_earlier_stage`. Последний случай структурный: `executor_runtime.initialize` заново проверяет stage с `verify_files=True` в момент старта его итерации, поэтому stage, который закрепил файл, переписываемый более ранним stage, был бы мёртв уже на итерации N. Каждый stage дополнительно проходит неизменённый `stage_contracts.validate_stage()`.

## Состояния

```
ready ──advance──► iteration_running ──► checkpoint_required
checkpoint_required ──checkpoint('continue')──► ready     [предыдущая итерация должна быть parent_accepted]
checkpoint_required ──checkpoint('stop')────► stopped      (терминально)
checkpoint_required ──redirect(...)─────────► redirected   (терминально; новый continuation loop = ready)
checkpoint_required ──accept(...)───────────► loop_accepted (терминально)
```

`advance` разрешён только из `ready`: из `checkpoint_required` он поднимает `checkpoint_required_before_advance`, из терминального статуса — `loop_terminal`, при исчерпанном бюджете — `loop_iteration_budget`. Статус `iteration_running` пишется **до** `executor_runtime.initialize` и никогда не возобновляется автоматически: следующий вход даёт `checkpoint_required` с причиной `interrupted_iteration_no_implicit_retry`.

`reason` объясняет, зачем нужен родитель: `iteration_ready_for_parent_review`, `iteration_needs_review`, `scope_ambiguity_returned_to_parent`, `no_admissible_stage`, `interrupted_iteration_no_implicit_retry`.

## Ledger вместо редактируемого состояния

Авторитет — `<loop_dir>/transitions/NNNN-<kind>.json`, create-only, никогда не перезаписывается. Закрытый набор из семи kind: `genesis`, `iteration_started`, `iteration_recorded`, `interrupted`, `checkpoint`, `redirect`, `accept`; жёсткий предел 32 записи на loop. Ничто другое не подписывается на этот ledger и не проецирует его.

`loop-state.json` — производная проекция без собственного авторитета. Перед каждым действием загрузка проверяет: связку `loop.json`/`registry.json`/`loop-seal.json` (`sealed_loop_changed`), непрерывность имён и последовательностей (`transition_ledger_gap`), форму записи (`transition_ledger_shape`), genesis (`transition_genesis_mismatch`), прямую цепочку хешей (`transition_chain_broken`), равенство `digest(state)` последнему `after_state_hash` (`loop_projection_stale`) и связь каждой записи `iterations` с ledger и с **сохранённым** состоянием run (`iteration_evidence_mismatch`). Правка одного `loop-state.json` отклоняется до любого действия.

Живой workspace при загрузке не читается: поздние итерации имеют право менять дерево, а родитель имеет право принять run вне loop до того, как `checkpoint()` это зафиксирует. Поэтому приёмка сверяется монотонно, а не на строгое равенство.

Порядок записи в каждом изменяющем действии: сначала авторитетные артефакты (каталог run, receipts, `redirect-receipt.json`, `loop-seal.json` continuation) — все create-only; затем `transitions/NNNN-<kind>.json` — это **точка фиксации**; затем атомарная перепубликация `loop-state.json`.

Артефакт, опубликованный до точки фиксации, может пережить сбой, до которого запись так и не дошла. Следующий вызов не заклинивает на create-only: он **сверяет** такой артефакт и продолжает ровно ту же незавершённую транзакцию. Условие принятия одно — точные canonical-байты файла связаны с той же вершиной ledger и тем же состоянием (`loop_hash`, `loop_state_hash`, `transition_hash`, `iteration`), а для selection-receipt дополнительно с тем же меню и той же парой `source`/`reason`, которую способен выдать `loop_menu.choose`. Всё остальное отклоняется закрыто прежними кодами: `selection_already_recorded`, `checkpoint_already_recorded`, `redirect_already_recorded`. Принятый selection-receipt сам является решением: повторного обращения к provider не происходит, и delta по jev берётся из уже записанного usage. После фиксации вершина ledger сдвигается, поэтому те же байты не могут быть приняты второй раз.

`loop-recover` разбирает ровно окно между точкой фиксации и перепубликацией: проекция уже текущая → `projection_current` без записи; проекция равна `before_state_hash` последней записи → воспроизводится **только** эта запись и публикуется результат (`projection_replayed`); иначе `loop_projection_unrecoverable`. Recovery никогда не запускает worker, deterministic action или provider: она лишь перепубликовывает то, что ledger уже зафиксировал.

## Redirect — неизменяемый continuation

`redirect()` не переписывает неизменяемый набор исходного loop. Под `loop-seal.json` проверяются статус и approval, затем открывается **одна** секция с marker `.staged-routing-workspace.json`, и в ней по порядку: сверка живого дерева с `carried_tree_hash` и записанным `tree_hash` (`redirect_workspace_changed`); `executor_runtime.inspect` должен подтвердить `parent_accepted` и тот же `result_hash` — только связанный хешами проверенный прогресс переносится; сверка workspace нового envelope (`continuation_workspace_mismatch`) и его полная валидация; публикация нового loop с двойным снимком дерева (`continuation_tree_drift`); create-only `redirect-receipt.json` (`redirect_already_recorded`); запись `redirect` и перепубликация проекции исходного loop как `redirected`.

Неизменяемый набор исходного loop побайтово совпадает до и после: `loop.json`, `registry.json`, `loop-seal.json`, все существовавшие `transitions/*.json`, всё под `iterations/**`, все существовавшие receipts. Новых путей ровно два (`redirect-receipt.json`, `transitions/<next>-redirect.json`), изменённый ровно один (`loop-state.json`). При любом расхождении дерева continuation не публикуется вовсе: ни каталог, ни receipt, ни запись ledger. `initial_tree_hash` continuation равен `carried_tree_hash`, то есть его первая итерация стартует ровно на проверенном дереве.

Прерванный redirect оставляет continuation в одном из двух состояний. **Опубликованный** (есть `loop-seal.json`) принимается только если его seal несёт ровно continuation этого незавершённого redirect — а она связана с `loop_state_hash` источника, его вершиной ledger и перенесённой evidence, — и его собственный ledger всё ещё состоит из одного genesis; тогда повторный вызов не создаёт ничего заново и лишь дописывает запись `redirect`. **Неопубликованный** остаток (seal нет, значит ни одна запись не может на него ссылаться) удаляется только если каждый его ограниченный член побайтово совпадает с тем, что записал бы этот вызов: `loop.json`, `registry.json` и два пустых каталога, и ничего сверх этого. Любой другой каталог на месте назначения — чужой: `continuation_conflict`, и он не изменяется и не удаляется.

## Публикация каталога через дескриптор

`no_links` доказывает, каким компонент пути **был**, а не каким он остался системным вызовом позже: родительский каталог назначения можно подменить символической ссылкой между проверкой и `mkdir`, и тогда все последующие пути разрешились бы внутри дерева атакующего. Поэтому родитель назначения разрешается ровно один раз, и удержанный дескриптор `O_DIRECTORY|O_NOFOLLOW` — а не путь — несёт создание самого каталога loop, его начальных членов (`loop.json`, `registry.json`, `transitions/`, `iterations/`, `loop-seal.json`, `transitions/0000-genesis.json`, `loop-state.json`) и удаление неопубликованного остатка. Подмена до открытия дескриптора отклоняется (`loop_parent_unavailable`), после — не уводит ни одного байта: запись идёт в проверенный каталог. Там, где платформа не даёт `dir_fd`-примитивов, публикация закрыта целиком (`directory_descriptor_unavailable`); отката на путевую запись нет.

Чтение при сверке — та же граница. Подменой можно заменить и **не последний** предок: переименование настоящего каталога не оставляет символической ссылки, поэтому `no_links` видит канонический путь, а `O_NOFOLLOW` и не ограничивал предков. Поэтому после того, как дескриптор назначения удержан, каждый байт уже опубликованного continuation читается только через него: seal, канонические `loop.json`/`registry.json`, проекция, обход ledger и run-state каждой записанной итерации — теми же валидаторами и с теми же кодами причин, что и публичный `inspect`. Ни один префикс пути назначения повторно не открывается, и после анкеровки путь не разрешается вовсе: авторитет — удержанный дескриптор. В запись `redirect` попадает `continuation_hash` — seal, прочитанный через этот дескриптор, — поэтому подмена предка после анкеровки не подставит в запись чужой loop и не отменит принятие нашего; `loop_directory` остаётся путём, который дал родитель, а continuation связывается с записью именно хешем. Специальный член (fifo, устройство) или отсутствующий член закрывают сверку, а не подвешивают её.

Цель и меню stage при redirect меняются — в этом его смысл. Workspace не меняется.

## Locks

Целей блокировки ровно две: `<loop_dir>/loop-seal.json` и `<workspace>/.staged-routing-workspace.json`; порядок всегда seal → marker, никогда наоборот и никогда дважды. `advance`, `checkpoint`, `accept` и `recover` держат только seal, потому что `executor_runtime.initialize` и `executor_runtime.advance` сами берут marker на новом дескрипторе, а вложенный захват в том же процессе даёт `one_writer_lock_busy`. `initialize` и `redirect` держат marker и поэтому внутри этой секции не вызывают ни один изменяющий путь `executor_runtime`; `executor_runtime.inspect` и `snapshot` блокировок не берут.

## CLI

```sh
python3.13 -B scripts/route_cli.py loop-validate   --envelope E --registry R --registry-sha256 S
python3.13 -B scripts/route_cli.py loop-init       --envelope E --registry R --registry-sha256 S --loop L --evidence-mode synthetic [--gate-evidence G]
python3.13 -B scripts/route_cli.py loop-advance    --loop L [--fixed-stage ID] [--fixed-route ID] [--allow-typesafe --grants F]
python3.13 -B scripts/route_cli.py loop-checkpoint --loop L --decision continue|stop [--approval F]
python3.13 -B scripts/route_cli.py loop-redirect   --loop L --new-loop L2 --envelope E2 --registry R --registry-sha256 S --approval F --evidence-mode synthetic
python3.13 -B scripts/route_cli.py loop-inspect    --loop L
python3.13 -B scripts/route_cli.py loop-recover    --loop L
python3.13 -B scripts/route_cli.py loop-accept     --loop L --approval F
```

`--registry-sha256` — SHA-256 **точных байтов файла** registry, а не canonical JSON. `--fixed-stage` выбирает заранее допущенный stage (это выбор *stage*), `--fixed-route` — допущенный executor внутри него (это выбор *route*); они ортогональны. Как и у `route-advance`, автоматического grant нет: `--allow-typesafe` сам по себе закрыт, потому что `OutboundAdmission` требует classifier родителя, которого CLI не предоставляет. Без `--fixed-route` итерация заканчивается на control route `owner`. Без `--fixed-stage` и без допущенного provider меню возвращает `no_admissible_stage`.

Форматы approval (точные наборы ключей):

```python
continue: {'decision':'continue','evidence_hash','reviewer','evidence_kind'}
redirect: {'decision':'redirect','evidence_hash','carried_tree_hash','reviewer','evidence_kind'}
accept:   {'decision':'accept','loop_evidence_hash','reviewer','evidence_kind'}   # digest(state['iterations'])
```

Exit codes: 0 — выполненное локальное действие родителя (`ready`, `redirected`, `loop_accepted`, `projection_current`, `projection_replayed`); 20 — `checkpoint_required`/`needs_review`/`stopped`, то есть родитель обязан посмотреть; 2 — blocked contract/environment. Exit 0 не означает client или live readiness.

## Границы

Все реальные executor routes остаются `unverified`/`disabled`; `assets/executor-routes.json` этим слоем не изменяется, и loop не может дойти до `luna_*` route. Финальная приёмка остаётся только за родителем: каждый receipt и каждая запись ledger несут `parent_acceptance: false`. Ledger доказывает, **что и в каком порядке зафиксировал контроллер**; он не является утверждением о качестве результата и не выдерживает атакующего, у которого есть право записи одновременно в ledger и в seal. `checkpoint_required` означает «родитель должен посмотреть»; `loop_accepted` означает, что живой рецензент связал evidence hash. Ни то, ни другое не является оценкой качества. Пороги Jev — стартовые настройки, не измеренная точность. Подробнее — в [протоколе проверки](verification.md).
