# Карта архитектуры «Клевера»

Это навигационный документ: он отвечает, где находится нужная логика и какие
тесты запускать. Подробные продуктовые invariants остаются в `AGENTS.md`, коде
и тестах. Технический аудит с измерениями и осознанно отложенными изменениями —
в `technical-audit-2026-09-02.md`.

## Runtime

Production запускает один Python-процесс:

```text
Telegram Mini App ─HTTP─> FastAPI / tma_api ─> domain services ─> SQLite
Telegram updates  ─────> aiogram polling
                                      └──────> Telegram Bot API
background workers ─────> lifecycle / outbox / reminders / UCL sync
```

`app/main.py` — composition root. Его lifespan создаёт Telegram clients,
polling и workers. Это не обычное stateless ASGI-приложение: второй Uvicorn
worker или вторая replica также запустит polling и все background loops.
Текущий deployment invariant — **один process и одна replica**.

Frontend статический: `tma/index.html`, `tma/app.js`, `tma/styles.css`. Node.js
не участвует в runtime и отдельного build pipeline нет.

## Направление зависимостей

Предпочтительное направление для нового кода:

```text
tournament_catalog / tma_contracts
                 ↓
domain services + focused stores + integrations
                 ↓
tma_api / workers / bot orchestration
                 ↓
main (wiring and lifecycle)
```

- `tournament_catalog.py` содержит только неизменяемые metadata шаблонов и
  раундов; он не зависит от БД, HTTP или сервисов.
- `tma_contracts.py` содержит только Pydantic request contracts; он не должен
  открывать БД или вызывать сервисы.
- Бизнес-команда должна жить в предметном модуле и сама определять transaction
  boundary. Endpoint занимается context/access, переводом ошибок и ответом.
- Telegram send не должен выполняться внутри бизнес-транзакции. Команда пишет
  durable intent/outbox, worker делает внешний side effect.
- Presentation serializer не должен становиться новым местом бизнес-правил.

Исторически границы не везде чистые: `contest_service.py`,
`shared_tournament_service.py` и `tma_api.py` всё ещё крупные, а некоторые
serializers читают БД. Разделять их нужно вертикальными сценариями с
characterization tests, а не механической нарезкой файлов.

## Компоненты и связанные тесты

| Область | Реализация | Основные тесты |
| --- | --- | --- |
| Composition root, health, static policy | `app/main.py` | `test_main.py`, `test_healthcheck_notifications.py` |
| TMA routes и orchestration | `app/tma_api.py` | `test_tma_api.py`, `test_*_api.py` |
| Request DTO и integer bounds | `app/tma_contracts.py` | `test_tma_contracts.py`, API tests |
| Контекст запуска и auth | `tma_context.py`, `tma_auth.py`, `tma_launch.py`, `tma_entrypoint.py` | одноимённые tests |
| Роли и Telegram admins | `access_control.py`, `supermoderator_service.py`, `telegram_username_resolver.py` | `test_access_control.py`, `test_supermoderator_service.py`, `test_telegram_username_resolver.py` |
| Локальные конкурсы и матчи | `contest_service.py` | `test_contest_service.py`, `test_two_legged_ties.py`, `test_champion_predictions.py`, `test_swiss_stage_predictions.py` |
| Общие турниры и fan-out | `shared_tournament_service.py` | `test_shared_tournaments.py`, `test_shared_two_legged_ties.py`, `test_shared_match_external_links.py` |
| Шаблоны и раунды | `tournament_catalog.py` | `test_tournament_catalog.py`, `test_database.py` |
| Сетка ЛЧ | `champions_league_bracket.py` | `test_champions_league_bracket.py`, `test_champions_league_rounds.py`, `test_ucl_bracket_safety.py` |
| football-data.org sync | `football_data_provider.py`, `champions_league_sync.py` | `test_football_data_provider.py`, `test_champions_league_sync.py` |
| Scoring | `scoring_service.py` и orchestration в contest/shared services | `test_scoring_service.py` плюс domain tests |
| Audit | `audit_service.py`, `audit_read_service.py` | `test_audit_events.py`, `test_audit_read_api.py` |
| Contest publications | `publication_outbox.py`, `publication_delivery.py`, `publication_worker.py`, render modules | `test_publication_state_machine.py`, `test_contest_publications.py`, `test_publication_message_matrix.py` |
| Legacy start publications | `match_prediction_publications.py` | `test_match_prediction_publications.py` |
| Prediction reminders | `prediction_reminder_store.py`, `prediction_reminders.py`, `prediction_reminder_worker.py` | три одноимённых test files |
| Match lifecycle | `match_lifecycle.py` | `test_match_lifecycle.py` |
| SQLite schema/connections | `database.py`, manual scripts | `test_database.py`, `test_*_migration.py` |
| Mini App | `tma/app.js`, `tma/styles.css` | `test_tma_*_ui.py`, `node --check tma/app.js` |

## Основные пути данных

### Чтение конкурса

1. `tma/app.js` вызывает `/api/tma/contests/{id}`.
2. `tma_api.py` проверяет подписанный chat context и доступ.
3. `contest_service.get_contest_details()` reconciles due matches, читает
   aggregate, visibility, scoring и leaderboard.
4. Serializer добавляет HTTP-представление, bracket и reminder state.
5. UI строит экран. До дедлайна backend не должен включать чужие прогнозы.

Этот путь пока возвращает крупный aggregate. Не удалять поля и не вводить lazy
history без response-contract и privacy tests.

### Локальная mutation

1. Pydantic DTO из `tma_contracts.py` валидирует transport input.
2. Endpoint получает `TmaContext` и effective role.
3. Команда в `contest_service.py` открывает transaction, повторно проверяет
   deadline/status/version и меняет aggregate.
4. В той же business transaction записываются audit/outbox intents, где это
   предусмотрено.
5. Endpoint переводит предметную ошибку в стабильный HTTP status/detail.

Frontend-проверка удобна для UX, но backend остаётся authoritative.

### Общий турнир

Global admin endpoint вызывает `shared_tournament_service.py`. Shared match,
tie, schedule и result материализуются локальными копиями во всех связанных
конкурсах. Прогнозы и рейтинги остаются локальными. Изменение result обязано
пересчитать каждую локальную копию, включая завершённые конкурсы.

Shared fan-out намеренно атомарен и может долго держать SQLite writer lock.
Не разрывать его без durable recovery design.

### Публикации

Команда создаёт или пересматривает durable publication intent. Worker claim-ит
revision, renderer строит сообщение, delivery отправляет его в Telegram и
фиксирует результат. Не смешивать этот pipeline с legacy
`match_prediction_publications.py`: у них пока разные ambiguity contracts.

### Напоминания

Store хранит settings, occurrences, deliveries, recipients и claims. Worker
выполняет reconciliation, claim, preflight, render и send. Timeout/cancellation
после начала Telegram call получает `unknown`, чтобы не отправлять сообщение
слепо повторно. Любое изменение этого state machine требует store + worker
tests, включая concurrent claim и schedule revision.

### Синхронизация Лиги чемпионов

`champions_league_sync.py` читает football-data.org, разрешает точные имена
команд, обновляет durable import/bracket state и вызывает shared commands.
Workflow специально состоит из восстанавливаемых коротких транзакций вокруг
внешнего I/O. Не оборачивать весь sync в одну SQLite transaction.

## Модель данных и транзакции

- `database.py` содержит только актуальную схему для новой БД; runtime не
  обновляет исторические схемы.
- Production migrations выполняются явными scripts и должны сохранять данные.
- Каждое connection включает foreign keys и busy timeout 5 s.
- `users` и `teams` глобальны; contest-owned данные удаляются cascade по
  documented product rules.
- SQLite допускает одного writer. Длинный `BEGIN IMMEDIATE`, сетевой вызов под
  lock и N+1 внутри write transaction — red flags.
- Connection нельзя создавать в одном thread и использовать в другом. При
  будущем `asyncio.to_thread` туда переносится целая синхронная команда.
- Не добавлять индекс без конкретного query plan/measurement.

## Где вносить изменение

- Новый или изменённый template default: `tournament_catalog.py`, затем
  storage CHECK/migration при новом key и `test_tournament_catalog.py`.
- Новое поле HTTP request: `tma_contracts.py`, endpoint в `tma_api.py`, API
  regression test. Не помещать domain validation только в DTO.
- Правило локального конкурса: `contest_service.py` и ближайший domain test.
- Правило общего турнира: `shared_tournament_service.py` плюс fan-out tests.
- Формула баллов: `scoring_service.py`; orchestration отдельно не дублирует
  формулу.
- Новый Telegram publication: publication outbox/state machine и renderer;
  не делать `send → save` прямо из endpoint.
- Напоминание: store отвечает за durable state, renderer — за содержимое,
  worker — за orchestration/Telegram ambiguity.
- Визуальное поведение: `tma/app.js`/`styles.css`; API остаётся источником
  доступа, дедлайнов и бизнес-ограничений.
- Изменение schema: `database.py` для новых установок, отдельный manual
  migration script для production и migration tests.

## Проверки

Минимум после любой Python-правки:

```powershell
python -m ruff format --check .
python -m ruff check .
python -m pytest
```

После TMA-правки дополнительно:

```powershell
node --check tma/app.js
```

Если `node` отсутствует в PATH Codex desktop, использовать bundled executable
из `load_workspace_dependencies`. Для локального изменения сначала запускаются
ближайшие tests из таблицы, затем полный suite.

## Зоны повышенного риска

- Telegram send ambiguity, retry и shutdown;
- shared result fan-out и bracket attach между несколькими aggregates;
- дедлайны и раскрытие чужих прогнозов;
- 90-minute football score отдельно от advancing team/extra time/penalties;
- historical templates и ручные production migrations;
- несколько process/replica;
- serializers, которые пока выполняют дополнительные DB reads;
- aggregate leaderboard payload `O(participants × predictions)`.

В этих областях сначала нужен characterization или fault-injection test, затем
малое изменение с явно описанным failure contract.
