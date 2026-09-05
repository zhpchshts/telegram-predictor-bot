from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from aiogram.exceptions import (
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import GetChat, SendMessage

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import create_match, create_world_cup_2026_contest
from app.database import database_connection, initialize_database
from app.prediction_reminder_store import (
    ReminderRenderRequest,
    RenderedReminderPart,
    save_reminder_preference,
    save_reminder_settings,
)
from app.prediction_reminder_worker import (
    ReminderPreflightRequest,
    TelegramPredictionReminderAdapter,
    process_due_prediction_reminders,
)
from tests.support import ensure_contest_teams


CHAT_ID = -1001234567890
NEW_CHAT_ID = -1009876543210
USER_ID = 123456789
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=CHAT_ID,
    telegram_user_id=USER_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


@dataclass
class StubPreflight:
    calls: int = 0

    async def prepare(self, _request: ReminderPreflightRequest) -> object:
        self.calls += 1
        return object()


class StubRenderer:
    def __init__(self, part_count: int) -> None:
        self.part_count = part_count
        self.calls = 0

    def render(
        self, _request: ReminderRenderRequest
    ) -> tuple[RenderedReminderPart, ...]:
        self.calls += 1
        return tuple(
            RenderedReminderPart(
                html=f"<p>Part {index}</p>",
                has_launch_button=index == self.part_count - 1,
            )
            for index in range(self.part_count)
        )


class RetrySecondPartSender:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.failed = False

    async def send(self, _preflight: object, part) -> int:
        self.calls.append(part.part_number)
        if part.part_number == 1 and not self.failed:
            self.failed = True
            raise TelegramRetryAfter(
                method=SendMessage(chat_id=CHAT_ID, text="reminder"),
                message="retry later",
                retry_after=1,
            )
        return 1000 + len(self.calls)


class NetworkFailureSender:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, _preflight: object, _part) -> int:
        self.calls += 1
        raise TelegramNetworkError(
            method=SendMessage(chat_id=CHAT_ID, text="reminder"),
            message="connection reset",
        )


def test_worker_processes_due_champion_deadline_without_matches(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name=None,
        username="anna",
        contest_name="Кубок",
        idempotency_key="prediction-reminder-worker-deadline",
        audit_actor=AUDIT_ACTOR,
    ).contest
    with database_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE contests
            SET champion_prediction_enabled = 1,
                champion_prediction_deadline_at = '2030-01-01T15:00:00.000000Z'
            WHERE id = ?
            """,
            (contest.id,),
        )
    save_reminder_settings(
        database_path=database_path,
        contest_id=contest.id,
        enabled=True,
        lead_time_minutes=180,
        now_utc=NOW,
    )

    class DeadlineRenderer:
        def __init__(self) -> None:
            self.calls = 0

        def render(
            self, request: ReminderRenderRequest
        ) -> tuple[RenderedReminderPart, ...]:
            self.calls += 1
            assert request.items == ()
            assert [
                (item.kind, item.deadline_at_utc) for item in request.deadlines
            ] == [("champion", "2030-01-01T15:00:00.000000Z")]
            return (RenderedReminderPart(html="<p>Champion deadline</p>"),)

    class SuccessfulSender:
        def __init__(self) -> None:
            self.calls = 0

        async def send(self, _preflight: object, _part) -> int:
            self.calls += 1
            return 9001

    renderer = DeadlineRenderer()
    sender = SuccessfulSender()
    asyncio.run(
        process_due_prediction_reminders(
            database_path=database_path,
            preflight=StubPreflight(),
            renderer=renderer,
            sender=sender,
            now_utc=NOW,
        )
    )

    assert renderer.calls == 1
    assert sender.calls == 1
    with database_connection(database_path) as connection:
        delivery_status = connection.execute(
            "SELECT status FROM prediction_reminder_deliveries"
        ).fetchone()[0]
        occurrence_status = connection.execute(
            "SELECT status FROM prediction_reminder_deadline_occurrences"
        ).fetchone()[0]
    assert delivery_status == "sent"
    assert occurrence_status == "sent"


def test_worker_resumes_only_pending_part_after_safe_429(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    _configured_due_match(database_path)
    preflight = StubPreflight()
    renderer = StubRenderer(part_count=2)
    sender = RetrySecondPartSender()

    asyncio.run(
        process_due_prediction_reminders(
            database_path=database_path,
            preflight=preflight,
            renderer=renderer,
            sender=sender,
            now_utc=NOW,
        )
    )
    asyncio.run(
        process_due_prediction_reminders(
            database_path=database_path,
            preflight=preflight,
            renderer=renderer,
            sender=sender,
            now_utc=NOW + timedelta(seconds=2),
        )
    )

    assert renderer.calls == 1
    assert sender.calls == [0, 1, 1]
    with database_connection(database_path) as connection:
        delivery = connection.execute(
            "SELECT status FROM prediction_reminder_deliveries"
        ).fetchone()
        parts = connection.execute(
            """
            SELECT part_number, status
            FROM prediction_reminder_delivery_parts
            ORDER BY part_number
            """
        ).fetchall()
    assert delivery["status"] == "sent"
    assert [(row["part_number"], row["status"]) for row in parts] == [
        (0, "sent"),
        (1, "sent"),
    ]


def test_worker_removes_revoked_opt_in_before_multipart_retry(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _configured_due_match(database_path)
    with database_connection(database_path) as connection:
        actor = connection.execute(
            "SELECT id FROM users WHERE telegram_user_id = ?", (USER_ID,)
        ).fetchone()
        chat = connection.execute(
            "SELECT id FROM chats WHERE telegram_chat_id = ?", (CHAT_ID,)
        ).fetchone()
        match = connection.execute(
            """
            SELECT matches.home_team_id
            FROM matches
            JOIN stages ON stages.id = matches.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ?
            """,
            (contest_id,),
        ).fetchone()
        assert actor is not None and chat is not None and match is not None
        actor_user_id = int(actor["id"])
        chat_id = int(chat["id"])
        connection.execute(
            """
            INSERT INTO champion_predictions (
                contest_id, user_id, predicted_team_id
            ) VALUES (?, ?, ?)
            """,
            (contest_id, actor_user_id, int(match["home_team_id"])),
        )
    save_reminder_preference(
        database_path=database_path,
        chat_id=chat_id,
        user_id=actor_user_id,
        mention_in_prediction_reminders=True,
        now_utc=NOW,
    )

    class MentionRenderer:
        def render(
            self, request: ReminderRenderRequest
        ) -> tuple[RenderedReminderPart, ...]:
            assert [recipient.telegram_user_id for recipient in request.recipients] == [
                USER_ID
            ]
            return (
                RenderedReminderPart(html="<p>already sent</p>"),
                RenderedReminderPart(
                    html=(
                        "<p>pending tail</p>"
                        "<p><b>Ждём прогнозы от:</b><br>"
                        f'<a href="tg://user?id={USER_ID}">Анна</a></p>'
                    )
                ),
            )

    class CapturingRetrySender:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str]] = []
            self.failed = False

        async def send(self, _preflight: object, part) -> int:
            self.calls.append((part.part_number, part.html))
            if part.part_number == 1 and not self.failed:
                self.failed = True
                raise TelegramRetryAfter(
                    method=SendMessage(chat_id=CHAT_ID, text="reminder"),
                    message="retry later",
                    retry_after=1,
                )
            return 2000 + len(self.calls)

    sender = CapturingRetrySender()
    asyncio.run(
        process_due_prediction_reminders(
            database_path=database_path,
            preflight=StubPreflight(),
            renderer=MentionRenderer(),
            sender=sender,
            now_utc=NOW,
        )
    )
    save_reminder_preference(
        database_path=database_path,
        chat_id=chat_id,
        user_id=actor_user_id,
        mention_in_prediction_reminders=False,
        now_utc=NOW,
    )
    asyncio.run(
        process_due_prediction_reminders(
            database_path=database_path,
            preflight=StubPreflight(),
            renderer=MentionRenderer(),
            sender=sender,
            now_utc=NOW + timedelta(seconds=2),
        )
    )

    assert [part_number for part_number, _html in sender.calls] == [0, 1, 1]
    retried_html = sender.calls[-1][1]
    assert f"tg://user?id={USER_ID}" not in retried_html
    assert "Ждём прогнозы от" not in retried_html


def test_worker_does_not_retry_ambiguous_network_send(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    _configured_due_match(database_path)
    sender = NetworkFailureSender()

    asyncio.run(
        process_due_prediction_reminders(
            database_path=database_path,
            preflight=StubPreflight(),
            renderer=StubRenderer(part_count=1),
            sender=sender,
            now_utc=NOW,
        )
    )
    asyncio.run(
        process_due_prediction_reminders(
            database_path=database_path,
            preflight=StubPreflight(),
            renderer=StubRenderer(part_count=1),
            sender=sender,
            now_utc=NOW + timedelta(minutes=1),
        )
    )

    assert sender.calls == 1
    with database_connection(database_path) as connection:
        status = connection.execute(
            "SELECT status FROM prediction_reminder_deliveries"
        ).fetchone()[0]
    assert status == "unknown"


def test_cancellation_after_send_started_is_recorded_as_unknown(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    _configured_due_match(database_path)
    started = asyncio.Event()

    class BlockingSender:
        async def send(self, _preflight: object, _part) -> int:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def exercise() -> None:
        task = asyncio.create_task(
            process_due_prediction_reminders(
                database_path=database_path,
                preflight=StubPreflight(),
                renderer=StubRenderer(part_count=1),
                sender=BlockingSender(),
                now_utc=NOW,
            )
        )
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())

    with database_connection(database_path) as connection:
        delivery_status = connection.execute(
            "SELECT status FROM prediction_reminder_deliveries"
        ).fetchone()[0]
        part_status = connection.execute(
            "SELECT status FROM prediction_reminder_delivery_parts"
        ).fetchone()[0]
    assert delivery_status == "unknown"
    assert part_status == "unknown"


def test_telegram_preflight_persists_chat_migration_before_keyboard(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    _configured_due_match(database_path)

    class MigratingBot:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def get_chat(self, chat_id: int) -> object:
            self.calls.append(chat_id)
            if chat_id == CHAT_ID:
                raise TelegramMigrateToChat(
                    method=GetChat(chat_id=CHAT_ID),
                    message="group upgraded",
                    migrate_to_chat_id=NEW_CHAT_ID,
                )
            return SimpleNamespace(
                id=NEW_CHAT_ID,
                type="supergroup",
                title="Новый чат",
            )

    bot = MigratingBot()
    adapter = TelegramPredictionReminderAdapter(
        bot=bot,
        database_path=database_path,
        bot_username="test_bot",
        bot_token="123:test-token",
    )
    result = asyncio.run(
        adapter.prepare(
            ReminderPreflightRequest(
                delivery_id=1,
                contest_id=1,
                telegram_chat_id=CHAT_ID,
            )
        )
    )

    assert bot.calls == [CHAT_ID, NEW_CHAT_ID]
    assert result.telegram_chat_id == NEW_CHAT_ID
    with database_connection(database_path) as connection:
        chat = connection.execute(
            "SELECT telegram_chat_id, title FROM chats"
        ).fetchone()
    assert chat["telegram_chat_id"] == NEW_CHAT_ID
    assert chat["title"] == "Новый чат"


def test_send_migration_is_safely_retried_on_next_claim(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    _configured_due_match(database_path)

    class MigratingSendBot:
        def __init__(self) -> None:
            self.sent_chat_ids: list[int] = []

        async def get_chat(self, chat_id: int) -> object:
            return SimpleNamespace(
                id=chat_id,
                type="supergroup",
                title="Новый чат" if chat_id == NEW_CHAT_ID else "Старый чат",
            )

        async def send_rich_message(self, chat_id: int, **_kwargs) -> object:
            self.sent_chat_ids.append(chat_id)
            if chat_id == CHAT_ID:
                raise TelegramMigrateToChat(
                    method=SendMessage(chat_id=CHAT_ID, text="reminder"),
                    message="group upgraded",
                    migrate_to_chat_id=NEW_CHAT_ID,
                )
            return SimpleNamespace(message_id=500)

    bot = MigratingSendBot()
    adapter = TelegramPredictionReminderAdapter(
        bot=bot,
        database_path=database_path,
        bot_username="test_bot",
        bot_token="123:test-token",
    )
    renderer = StubRenderer(part_count=1)
    asyncio.run(
        process_due_prediction_reminders(
            database_path=database_path,
            preflight=adapter,
            renderer=renderer,
            sender=adapter,
            now_utc=NOW,
            max_deliveries=1,
        )
    )
    with database_connection(database_path) as connection:
        first_delivery = connection.execute(
            "SELECT status FROM prediction_reminder_deliveries"
        ).fetchone()[0]
        first_part = connection.execute(
            "SELECT status FROM prediction_reminder_delivery_parts"
        ).fetchone()[0]
    assert first_delivery == "retry"
    assert first_part == "pending"

    asyncio.run(
        process_due_prediction_reminders(
            database_path=database_path,
            preflight=adapter,
            renderer=renderer,
            sender=adapter,
            now_utc=NOW + timedelta(seconds=2),
            max_deliveries=1,
        )
    )

    assert bot.sent_chat_ids == [CHAT_ID, NEW_CHAT_ID]
    with database_connection(database_path) as connection:
        status = connection.execute(
            "SELECT status FROM prediction_reminder_deliveries"
        ).fetchone()[0]
        chat_id = connection.execute("SELECT telegram_chat_id FROM chats").fetchone()[0]
    assert status == "sent"
    assert chat_id == NEW_CHAT_ID


def _configured_due_match(database_path: Path) -> int:
    initialize_database(database_path)
    contest = create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name=None,
        username="anna",
        contest_name="Кубок",
        idempotency_key="prediction-reminder-worker-contest",
        audit_actor=AUDIT_ACTOR,
    ).contest
    team_ids = ensure_contest_teams(
        database_path,
        contest_id=contest.id,
        names=("Альфа", "Бета"),
    )
    create_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name=None,
        username="anna",
        home_team_id=team_ids[0],
        away_team_id=team_ids[1],
        starts_at_utc="2030-01-01T15:00:00Z",
        idempotency_key="prediction-reminder-worker-match",
        audit_actor=AUDIT_ACTOR,
    )
    save_reminder_settings(
        database_path=database_path,
        contest_id=contest.id,
        enabled=True,
        lead_time_minutes=180,
        now_utc=NOW,
    )
    return contest.id
