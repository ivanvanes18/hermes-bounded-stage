# Luna Codex executor adapter (`scripts/luna_codex_adapter.py`)

Первый **real-capable** адаптер протокола `hermes-executor-v1`: закреплённый (pinned) подпроцесс, который управляет установленным `codex app-server` по stdio JSON-RPC, выполняет ровно один ограниченный read-only turn на `gpt-5.6-luna` с reasoning effort `max` и возвращает честные свидетельства identity / session / permissions / usage — либо не возвращает ничего.

**Поставляется выключенным.** `assets/executor-routes.json` не изменён: все реальные executor routes остаются `status:"unverified"`, `adapter:null`, `model:null`. Допуск к живому пилоту происходит отдельно, через приватный файл реестра вне репозитория, передаваемый существующими флагами `--registry / --registry-sha256`. Установка скилла сама по себе ничего не активирует.

## Контракт вызова

```
argv        = [<abs python3.13 ELF>, '-I', '-S', <abs luna_codex_adapter.py>,
               <abs codex ELF>, *prefix,
               '--expected-codex-version', <точная строка `codex --version`>,
               '--probe-anchor', <abs probe anchor file>]
pins        = {python3.13, luna_codex_adapter.py, codex ELF, probe anchor, *абсолютные prefix-элементы}
native_pins = [<abs codex ELF>]
```

- `sys.argv[1]` — fd-alias исполняемого файла Codex; `sys.argv[2:-4]` — prefix (в production пустой).
- Последние четыре элемента обязаны быть ровно `--expected-codex-version <строка>` и `--probe-anchor <путь>`, в этом порядке. Иначе — `argv_shape`. Режима «версия не привязана» не существует.
- **Версия привязана точно, а не как минимум.** `--expected-codex-version` — это дословная строка `codex-cli <semver>`, измеренная у того самого ELF, чей SHA-256 попал в `pins`; `tools/pilot_registry.py` получает её, запустив `/proc/self/fd/<n>` удерживаемого дескриптора, без обращения к модели. Она является элементом argv, поэтому входит в `adapter_digest`, а значит и в `approval_sha256`. И probe, и run требуют **строгого равенства** наблюдённой строки с ней (`cli_version_mismatch`); `MIN_CLI_VERSION` остаётся только нижним порогом и не может расширить множество принимаемых версий. Обновление Codex под тем же путём делает спецификацию неприменимой до нового одобрения — это и есть цель.
- Ни один элемент версионной пары не является абсолютным путём, поэтому ни один из них не становится пином; оба являются argv, поэтому оба находятся внутри `adapter_digest`.
- `harness_adapter.readiness` вычисляет cwd для probe как `Path(spec['argv'][-1]).parent` (`harness_adapter.py:93`). Якорь — последний элемент argv именно поэтому: probe выполняется в одноразовом каталоге якоря, вне репозитория, вне workspace и вне дерева пакета Codex.
- Якорь **проверяется и отбрасывается**: обычный файл, ≤ 4096 байт, содержимое ровно `{"purpose":"hermes-luna-probe-anchor","schema_version":1}`. Иначе — `probe_anchor_invalid`. Codex его никогда не получает, и его дескриптор исключён из `pass_fds`.

`_fd_aliases(argv)` собирает все элементы вида `/proc/self/fd/<n>` и передаёт эти дескрипторы через `pass_fds=`. Без этого CPython закрывает унаследованные дескрипторы непосредственно перед `execv`, и alias Codex перестаёт разрешаться.

## Что именно закреплено, а что — нет

Закреплено (SHA-256, `O_NOFOLLOW` по каждому компоненту пути, запечатанный memfd, повторное хеширование после запечатывания) ровно **четыре** объекта:

1. `/usr/bin/python3.13` — нативный ELF-интерпретатор, `argv[0]`, снимок в режиме `0500`;
2. `scripts/luna_codex_adapter.py` — исходник адаптера, снимок `0400`;
3. нативный ELF Codex — через `native_pins`, снимок `0500`;
4. файл probe anchor — снимок `0400`.

**Доверенные внешние предпосылки — они НЕ проверяются по идентичности ничем в этом слое:** стандартная библиотека Python, динамический загрузчик, libc, `/proc`; **вся npm-поставка Codex и её замыкание зависимостей** (`@openai/codex`, `codex.js`, `node_modules/`, vendor-дерево `codex-linux-x64` и любой файл, который ELF Codex открывает, отображает, запускает или скачивает во время работы); `~/.codex/` (`auth.json`, `config.toml`, кеши, плагины, агенты); ядро ОС, его поведение изоляции и сетевой путь до любого провайдера.

Закрепление объекта 3 доказывает, *какие байты исполняются как точка входа Codex*. Оно ничего не доказывает о том, что эти байты затем читают или исполняют. **Рекурсивного утверждения об идентичности пакета не делается и делаться не может.** Фраза «Codex закреплён» верна только относительно ELF точки входа и должна писаться именно так.

Скилл **не является песочницей**. Закрепление — это контроль процесса и идентичности байтов, а не изоляция файловой системы или сети.

## Константы — обозримая поверхность ревью

| Константа | Значение |
|---|---|
| `MODEL` / `EFFORT` | `gpt-5.6-luna` / `max` |
| `SANDBOX_MODE` / `SANDBOX_POLICY` | `read-only` / `{'type':'readOnly','networkAccess':False}` |
| `APPROVAL_POLICY` / `APPROVALS_REVIEWER` / `THREAD_SOURCE` | `never` / `user` / `user` |
| `EFFORT_CONFIG_KEY` | `model_reasoning_effort` |
| `MIN_CLI_VERSION` | `(0, 153, 0)` — только нижний порог; решает точное равенство с `--expected-codex-version` |
| `EXPECTED_VERSION_FLAG` | `--expected-codex-version` |
| `MAX_LINE` / `MAX_TOTAL_RX` / `MAX_NOTIFICATIONS` | 1 MiB / 32 MiB / 4096 |
| `MAX_PROMPT_BYTES` / `VERSION_TIMEOUT` / `SESSION_TIMEOUT` | 64 KiB / 10 с / 110 с |
| `SUPPORTED_MODES` / `EVIDENCE_KIND` | `('read_only',)` / `live` |

`EFFORT_CONFIG_KEY` — единственное значение, которое не удалось доказать офлайн: `ThreadStartParams.config` объявлен `additionalProperties: true`, поэтому протокольный слой **не отклонит** неверный ключ. Он поэтому **не считается доверенным**: адаптер проверяет `ThreadStartResponse.reasoningEffort == 'max'`, так что неверный ключ вырождается в чистый отказ `effort_not_proven`, а не в тихий прогон на effort по умолчанию.

## Окружение дочернего процесса

Строится из литерального словаря на основе `pwd.getpwuid(os.getuid()).pw_dir` — никогда не наследуется: `PATH`, `HOME`, `CODEX_HOME`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR` (временный каталог удаляется в `finally`). Ни одна переменная Hermes, gateway, GitHub, облака, прокси или провайдера не пересылается. Дочерний процесс **не** получает новую сессию: он остаётся в группе процессов harness, и именно это позволяет `os.killpg` (`harness_adapter.py:67`) снять его вместе с потомками.

## Последовательность прогона и таблица проверки

1. `initialize` с `{clientInfo}` → требуются `{codexHome, platformFamily, platformOs, userAgent}`, `platformOs == 'linux'`, `codexHome == CODEX_HOME`.
2. **`initialized`** — уведомление, единственный член замороженного `ClientNotification`. Обязательная часть рукопожатия, отправляется до любого следующего запроса, и на probe, и на run.
3. `thread/start` с `{model, cwd, sandbox:'read-only', approvalPolicy, approvalsReviewer, threadSource, ephemeral:true, config:{model_reasoning_effort:'max'}}`.
4. Проверка `ThreadStartResponse`. **Каждая строка отказывает закрыто:**

| Поле | Требуемое значение | Код отказа |
|---|---|---|
| `model` | `gpt-5.6-luna` | `model_mismatch` |
| `reasoningEffort` | `max`, присутствует и не null | `effort_not_proven` |
| `cwd` | `os.path.realpath(workspace)` | `cwd_mismatch` |
| `sandbox.type` | `readOnly` | `sandbox_mismatch` |
| `sandbox.networkAccess` | **присутствует и `is False`** | `sandbox_network_not_proven` |
| `approvalPolicy` | `never` | `approval_policy_mismatch` |
| `approvalsReviewer` | `user` | `reviewer_mismatch` |
| `thread.threadSource` | `user` | `thread_source_mismatch` |
| `thread.id`, `thread.sessionId` | непустые строки | `session_binding_missing` |
| `thread.model`, `thread.reasoningEffort` | равны проверенным выше | `thread_identity_mismatch` |

5. `turn/start` с `{threadId, input:[{type:'text', text}], effort:'max', model, cwd, sandboxPolicy, approvalPolicy, turnTrigger}`.
6. Drain **до появления обоих** свидетельств под одним монотонным дедлайном: подходящего терминального `turn/completed` **и** подходящего `thread/tokenUsage/updated`. Ни одно из них по отдельности цикл не завершает, и порядок их прихода не предполагается ни в одну сторону.
7. `turn/interrupt` на любом пути отказа; затем закрытие stdin, завершение процесса, удаление временного каталога — всё в `finally`.

### Область видимости уведомлений — формы различаются

| Уведомление | Thread id в | Turn id в |
|---|---|---|
| `thread/started` | `params.thread.id` | — |
| `turn/started` | `params.threadId` | **`params.turn.id`** |
| `turn/completed` | `params.threadId` | **`params.turn.id`** |
| `thread/tokenUsage/updated` | `params.threadId` | **`params.turnId`** |

`TurnStartedNotification` и `TurnCompletedNotification` имеют обязательные `{threadId, turn}` и **вообще не содержат `params.turnId`**. Привязка всех turn-уведомлений к `params.turnId` привела бы к тому, что `turn/completed` никогда не совпадает и каждый реальный прогон падает с `turn_not_completed`.

### Политика уведомлений и серверных запросов

- **Чужой thread, чужой turn, id из неверного поля, отсутствующие id, неизвестный метод** — учитываются как отброшенные и игнорируются. Состояние не продвигают, свидетельств не дают.
- **`error`**, привязанный к нашему thread → `server_error_notification`.
- **Любой входящий `ServerRequest`** (их ровно десять в замороженном `ServerRequest.json`) → ответ `{"code":-32001,"message":"approval_declined"}`, затем `turn/interrupt`, затем закрытый отказ `server_request_declined`. У ограниченного read-only прогона нет законной причины быть о чём-либо спрошенным; сам факт запроса означает, что пакет подтолкнул модель к побочному эффекту.
- **Терминальный статус.** Требуется `turn/completed` для нашей пары `(threadId, turn.id)` со `status == 'completed'`. `interrupted` / `failed` / отсутствие / дедлайн → `turn_not_completed`. `TurnStartResponse.turn.status` (обычно `inProgress`) не считается доверенным никогда.
- **Usage.** Из **последнего** подходящего `thread/tokenUsage/updated`, поле `tokenUsage.total`: `input_tokens = total.inputTokens`, `output_tokens = total.outputTokens`, оба обязательны, приводятся к `int` и проверяются в диапазоне `0..1_000_000_000`. Отсутствие подходящего уведомления ⇒ `usage_evidence_missing`. **Нули никогда не выдумываются.**
- **Текст ассистента.** Каждая `item/*`-дельта и финальное сообщение отбрасываются без разбора.

## Промпт — только из пакета

`_prompt(packet)` детерминирован и строится ровно из: `objective`, `mode`, `allowed_paths`, `forbidden`, `domain_rules[*].{skill,rule_id,text}`, `output_contract.required_paths`, `inputs[*].{id,path,sha256}`, `stop_conditions`, `selected_context[*].{id,source_sha256,text}`, `execution.attempt`, `execution.correction_evidence`, плюс фиксированная преамбула.

**Никогда в промпте:** `nonce`, `packet_hash`, `route_receipt_hash`, `stage_hash`, `methodology_hash`, идентификаторы маршрутов, исходные пути `workflow.*`, путь якоря и что-либо из окружения самого адаптера. Превышение 64 KiB → `prompt_budget`.

## Probe — без обращения к модели

1. `<codex-alias> *prefix --version`, строгое окружение, ≤ 10 с. Неразбираемое — `cli_version_unreadable`. Затем **точное равенство** со строкой из `--expected-codex-version`, иначе `cli_version_mismatch`; и лишь затем нижний порог `>= 0.153.0` (`cli_version_unsupported`). **Тот же шаг выполняется и на пути run**, перед `app-server`: readiness может быть многоминутной давности, а исполняемый файл за alias заслуживает доверия ровно настолько, насколько заслуживает доверия версия, которую он сообщает в момент старта turn.
2. `app-server`, `initialize`, проверка формы `InitializeResponse` и `codexHome`, завершение. **Ни `thread/start`, ни `turn/start`, ни одного обращения к модели.**
3. Ответ ровно: `{"protocol":"hermes-executor-v1","operation":"probe","ready":true,"identity":{"harness":"luna","model":"gpt-5.6-luna","effort":"max"},"supported_modes":["read_only"],"evidence_kind":"live"}`.

Identity в probe — это собственные закреплённые константы адаптера плюс доказанная проверка возможностей CLI: честная граница. Доказывает, что модель действительно обслужила turn, **только run**.

## Дисциплина вывода

- Успех: ровно один JSON-объект на stdout (`sort_keys`, `ensure_ascii=False`, компактные разделители, без NaN), больше ничего, код выхода 0.
- Отказ: **ничего на stdout**, одна строка кода причины на stderr, код выхода 1. Harness превращает ненулевой выход в `worker_exit_failure` и отбрасывает stderr на уровне ОС (`stderr=DEVNULL`, `harness_adapter.py:32`), поэтому контроллер не сохраняет ни одной диагностики.
- Частичный или оптимистичный конверт не пишется никогда. Отсутствие identity, usage, session или терминального свидетельства идёт этим же путём.

## Границы, которые нельзя размывать

- **`sandbox.networkAccess` строг намеренно.** `ReadOnlySandboxPolicy` требует только `type`; поле несёт `"default": false` и **может законно отсутствовать на проводе**. Адаптер не принимает отсутствие за `false`, потому что `proof.permissions.network` утверждается контроллеру как `False`, а отсутствующее поле — не наблюдение. **Значение по умолчанию не является свидетельством.** Если реальный сервер 0.153.4 поле не присылает, правильный исход — что `luna_max_read_only` **никогда не допускается**. Ни отката, ни вывода из `sandbox.type`, ни флага, который это ослабляет, нет.
- **Три поля `Thread` необязательны в схеме и обязательны здесь.** `Thread.model`, `Thread.reasoningEffort` и `Thread.threadSource` необязательны и nullable в 0.153.4. Требовать их — намеренный выбор отказа закрыто: thread, который не подтверждает собственную идентичность, не даёт доказательства.
- **`reasoningEffort` — сконфигурированное состояние thread, а не телеметрия исполнения turn.** Схема 0.153.4 говорит дословно: *«Current configured reasoning effort when loaded, otherwise the latest persisted effort. Null when unset or unavailable. This is not per-turn execution telemetry.»* `proof.effort` поэтому свидетельствует *об effort, с которым был сконфигурирован thread*, а не о том, что turn исполнился на этом effort. Ничто в этом слое этот разрыв не закрывает.
- **`network:false` — это разрешение инструментов worker, а не утверждение о транспорте.** Оно не значит, что Codex не может обратиться в сеть.
- **Доверенный адаптер может солгать о собственной идентичности.** На этом уровне это не исправляется (`references/adapter-protocol.md`). Смягчается закреплением, запечатыванием, человеческим одобрением и использованием наблюдённых, а не запрошенных значений.
- Синтетический fake app-server — свидетельство о *протокольной обработке этого адаптера*, его stdio-транспорте и проверке конвертов контроллером. Это **не** свидетельство о реальной файловой песочнице Codex, о сетевом выходе провайдера и о том, что заполняет настоящий `codex app-server`.

## Как производится приватный реестр пилота (`tools/`, не поставляется)

`tools/` намеренно вне бандла скилла: оттуда ничего не устанавливается и ничто не достижимо из установленного скилла. Общий слой ввода-вывода — `tools/pilot_io.py`; им пользуются и `pilot_registry.py`, и `pilot_preflight.py`.

Каталог якоря — **предпосылка, а не побочный эффект**: инструмент его не создаёт. Перед первым запуском создайте и закройте его вручную:

```bash
mkdir -p ~/.hermes/pilot/probe-anchor
chmod 700 ~/.hermes/pilot ~/.hermes/pilot/probe-anchor    # если ещё не 0700
ANCHOR=~/.hermes/pilot/probe-anchor/anchor.json           # сам файл создаёт pilot_registry.py
```

- **Пути разрешаются, а не интерпретируются.** Каждый абсолютный родительский компонент открывается с `O_NOFOLLOW|O_DIRECTORY` от дескриптора на `/`, поэтому ни один предок не может быть символической ссылкой и ни один предок не может быть подменён между проверкой и использованием: дальше всё делается относительно удерживаемого дескриптора. `.`, `..` и пустые компоненты отклоняются, а не нормализуются.
- **Приватность — это режим, а не договорённость.** Конечный родитель обязан принадлежать текущему uid и не иметь **ни одного** group/other-бита (`st_mode & 0o077 == 0`; используемый режим — ровно `0700`). Родители никогда не создаются: отсутствующий родитель — отказ (`path_component_missing`), потому что создать его значило бы писать в пространство имён, которое никто не проверял. Режим публикуемого файла — **явный аргумент из закрытого множества** `{0400, 0600}`; любое другое значение — `mode_not_restricted` ещё до появления дескриптора. Реестр и вывод preflight остаются `0600`, закрепляемый якорь probe — `0400`. Точный итоговый режим сверяется при перечитывании, поэтому публикация `0400` не удовлетворяется файлом `0600`.
- **Якорь probe готовится через ту же границу.** `prepare_anchor` не пользуется путевыми `exists` / `read_bytes` / `chmod` и не создаёт родительский каталог ни рекурсивно, ни иначе: родитель обязан существовать заранее и быть приватным, и достигается тем же покомпонентным `O_NOFOLLOW`-обходом от `/`. Существующий якорь читается через удерживаемый дескриптор и обязан быть ровно байтами стуба, принадлежать текущему uid и иметь режим ровно `0400`; иначе — `source_symlink`, `source_not_regular`, `source_not_owned`, `source_not_private`, `source_mode`, `source_too_large` или `probe_anchor_not_stub`. Отсутствующий якорь публикуется относительно дескриптора каталога через `O_CREAT|O_EXCL` с полным циклом записи, `fsync` файла и родителя и перечитыванием по байтам, владельцу, режиму и inode. Единственное условие, при котором что-то записывается, — отсутствие имени; всякий другой отказ распространяется как есть.
- **Публикация никогда не перезаписывает имя.** Ни `os.replace`, ни режима `w`, ни усечения. Существующая цель — символическая ссылка, обычный файл, FIFO или что угодно ещё — это отказ. Запись идёт относительно дескриптора каталога через `O_CREAT|O_EXCL|O_NOFOLLOW`, полным циклом записи, с `fsync` файла и затем родителя, после чего имя открывается заново через новый `O_NOFOLLOW`-дескриптор и сверяются точные байты, владелец, режим и **inode**. Имя, перенаправленное на другой файл между записью и перечитыванием, ловится сравнением inode, а не только содержимого. Незавершённая публикация удаляется, поэтому отказ не оставляет следов.
- **Исполняемый файл удерживается, а не называется.** `hold_executable` возвращает открытый дескриптор на измеренные байты: обычный файл, без setuid/setgid, с битом исполнения владельца, с сигнатурой `\x7fELF`, хешируемый через этот же дескриптор. Дальше запускается `/proc/self/fd/<n>` с `pass_fds=`, поэтому наблюдённая версия и запущенный `app-server` происходят из того inode, чей SHA-256 попал в `pins`. Замена имени пути после открытия ничего подменить не может.
- **Запись реестра привязана к свидетельствам.** `pilot_registry.py` требует `--evidence-bundle` — приватный обычный файл, читаемый и хешируемый через удерживаемый дескриптор; его SHA-256 обязан совпасть с полем `evidence_bundle_sha256` одобрения. Одобрение сверяется **поле за полем** с величинами, измеренными в этом же процессе: `route_id`, `approved_by`, `purpose`, `harness`, `model`, `effort`, `mode`, `evidence_kind`, `scope`, `data_classification`, точная `cli_version`, хеши python / адаптера / Codex / якоря / набора свидетельств, `adapter_digest`, `approved_at` и `expires_at`. Неизвестные ключи, отсутствующие ключи, `null` и несовпадение типа — отказ. `approval_sha256 = sha256(canonical(record))` берётся с уже проверенной записи, и только этот дайджест попадает в маршрут.

Что это **не** доказывает: ничего о содержимом измеренного исполняемого файла, ничего о том, что этот файл затем открывает или запускает, и ничего о каталогах выше конечного родителя сверх того, что это настоящие каталоги, а не символические ссылки.
