from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    create_champions_league_2026_27_contest,
    get_contest_details,
    save_swiss_stage_prediction,
    save_swiss_stage_prediction_settings,
    save_tournament_teams,
)
from app.database import initialize_database
from app.healthcheck_notifications import (
    get_healthcheck_snapshot,
    send_healthcheck_notification,
)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, chat_id: int, *, text: str) -> object:
        message = {"chat_id": chat_id, "text": text}
        self.messages.append(message)
        return message


CHAT_ID = -1001234567890
ADMIN_ID = 101
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=CHAT_ID,
    telegram_user_id=ADMIN_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)
OPEN_TIME = datetime(2029, 1, 1, tzinfo=timezone.utc)


def test_healthcheck_snapshot_counts_empty_active_state(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    snapshot = get_healthcheck_snapshot(database_path=database_path)

    assert snapshot.active_contests_count == 0
    assert snapshot.active_matches_count == 0
    assert snapshot.saved_predictions_count == 0


def test_healthcheck_counts_partial_but_not_cleared_general_stage_prediction(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest_id = create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=ADMIN_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        contest_name="Лига чемпионов 2026/27",
        idempotency_key="healthcheck-ucl-contest",
        audit_actor=AUDIT_ACTOR,
    ).contest.id
    save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        team_names=[f"Команда {number:02d}" for number in range(1, 37)],
        audit_actor=AUDIT_ACTOR,
    )
    save_swiss_stage_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        enabled=True,
        deadline_at="2030-01-01T12:00:00Z",
        direct_qualifier_count=8,
        elimination_qualifier_count=12,
        audit_actor=AUDIT_ACTOR,
        now_utc=OPEN_TIME,
    )
    team_ids = tuple(
        team.id
        for team in get_contest_details(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            now_utc=OPEN_TIME,
        ).swiss_stage_prediction.candidates
    )
    save_swiss_stage_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=202,
        first_name="Алиса",
        last_name=None,
        username="alice",
        direct_team_ids=[team_ids[0]],
        elimination_team_ids=[],
        now_utc=OPEN_TIME,
    )
    save_swiss_stage_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=303,
        first_name="Боб",
        last_name=None,
        username="bob",
        direct_team_ids=[team_ids[1]],
        elimination_team_ids=[],
        now_utc=OPEN_TIME,
    )
    save_swiss_stage_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=303,
        first_name="Боб",
        last_name=None,
        username="bob",
        direct_team_ids=[],
        elimination_team_ids=[],
        now_utc=OPEN_TIME,
    )

    snapshot = get_healthcheck_snapshot(database_path=database_path)

    assert snapshot.active_contests_count == 1
    assert snapshot.saved_predictions_count == 1


def test_healthcheck_notification_sends_status_message(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    bot = FakeBot()

    asyncio.run(
        send_healthcheck_notification(
            bot=bot,
            database_path=database_path,
            chat_id=123,
        )
    )

    assert len(bot.messages) == 1
    message = bot.messages[0]
    assert message["chat_id"] == 123
    assert "✅ Клевер работает." in message["text"]
    assert "Время сервера UTC:" in message["text"]
    assert "Активных конкурсов: 0" in message["text"]
    assert "Предстоящих и идущих матчей: 0" in message["text"]
    assert "Сохранённых прогнозов: 0" in message["text"]
