from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage
import pytest

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    create_champions_league_2026_27_contest,
    create_match,
    create_world_cup_2026_contest,
    save_champion_prediction_settings,
    save_swiss_stage_prediction_settings,
    save_tournament_teams,
)
from app.database import database_connection, initialize_database
from app.prediction_reminders import (
    NoOpenPredictionRemindersError,
    PredictionReminderMessageTooLongError,
    build_prediction_reminder_message,
    publish_prediction_reminders,
)
from tests.support import ensure_contest_teams


CHAT_ID = -1001234567890
USER_ID = 123456789
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=CHAT_ID,
    telegram_user_id=USER_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_rich_message(
        self,
        chat_id: int,
        *,
        rich_message: InputRichMessage,
        reply_markup: InlineKeyboardMarkup,
    ) -> SentMessage:
        self.sent.append(
            {
                "chat_id": chat_id,
                "rich_message": rich_message,
                "reply_markup": reply_markup,
            }
        )
        return SentMessage(message_id=1000 + len(self.sent))


def test_reminder_collects_open_long_term_predictions_and_future_matches(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _configured_contest(database_path)

    message = build_prediction_reminder_message(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=NOW,
    )

    assert message.telegram_chat_id == CHAT_ID
    assert message.reminder_count == 4
    assert message.match_count == 2
    assert "Конкурс: «Кубок &lt;друзей&gt; &amp; коллег»" in message.html
    assert "Прогноз на швейцарский этап" in message.html
    assert "Дедлайн: 02.01.2030, 12:00 UTC" in message.html
    assert "Прогноз на чемпиона" in message.html
    assert "Дедлайн: 03.01.2030, 12:00 UTC" in message.html
    assert "Альфа &lt;1&gt; — Бета &amp; 2" in message.html
    assert "Начало: 04.01.2030, 15:30 UTC" in message.html
    assert "Гамма — Дельта" in message.html
    assert "Начало: 05.01.2030, 18:00 UTC" in message.html
    assert "Прошедшая — Встреча" not in message.html
    assert "Отменённая — Игра" not in message.html
    assert "Начатая — Серия" not in message.html
    assert message.html.index("Прогноз на швейцарский этап") < message.html.index(
        "Предстоящие матчи"
    )


def test_publish_sends_exactly_one_prebuilt_rich_message(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _configured_contest(database_path)
    bot = RecordingBot()
    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сделать прогноз",
                    url="https://t.me/test_bot?startapp=test",
                )
            ]
        ]
    )

    message = asyncio.run(
        publish_prediction_reminders(
            bot=bot,
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            reply_markup=reply_markup,
            now_utc=NOW,
        )
    )

    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == CHAT_ID
    rich_message = bot.sent[0]["rich_message"]
    assert isinstance(rich_message, InputRichMessage)
    assert rich_message.html == message.html
    assert rich_message.skip_entity_detection is True
    assert bot.sent[0]["reply_markup"] is reply_markup


def test_champions_league_reminder_calls_swiss_prediction_league_phase(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "champions-league.db"
    initialize_database(database_path)
    contest = create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name=None,
        username="anna",
        contest_name="Лига чемпионов 2026/27",
        idempotency_key="ucl-reminder-contest",
        audit_actor=AUDIT_ACTOR,
    ).contest
    save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        team_names=[f"Команда {number:02d}" for number in range(1, 37)],
        audit_actor=AUDIT_ACTOR,
    )
    save_swiss_stage_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        enabled=True,
        deadline_at="2030-01-02T12:00:00Z",
        direct_qualifier_count=8,
        elimination_qualifier_count=12,
        audit_actor=AUDIT_ACTOR,
        now_utc=NOW,
    )

    message = build_prediction_reminder_message(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        now_utc=NOW,
    )

    assert message.reminder_count == 1
    assert message.match_count == 0
    assert "Прогноз на лиговый этап" in message.html
    assert "швейцарский этап" not in message.html.lower()


def test_closed_predictions_and_started_matches_are_not_published(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _configured_contest(database_path)

    with pytest.raises(
        NoOpenPredictionRemindersError,
        match="Нет открытых прогнозов или предстоящих матчей",
    ):
        build_prediction_reminder_message(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            now_utc=datetime(2031, 1, 1, tzinfo=timezone.utc),
        )


def test_message_is_not_partially_published_when_it_exceeds_one_message(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _configured_contest(database_path)

    with pytest.raises(
        PredictionReminderMessageTooLongError,
        match="не помещаются в одно сообщение",
    ):
        build_prediction_reminder_message(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            now_utc=NOW,
            max_message_length=100,
        )


def _configured_contest(database_path: Path) -> int:
    initialize_database(database_path)
    contest = create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name=None,
        username="anna",
        contest_name="Кубок <друзей> & коллег",
        idempotency_key="prediction-reminder-contest",
        audit_actor=AUDIT_ACTOR,
    ).contest
    team_ids = ensure_contest_teams(
        database_path,
        contest_id=contest.id,
        names=(
            "Альфа <1>",
            "Бета & 2",
            "Гамма",
            "Дельта",
            "Прошедшая",
            "Встреча",
            "Отменённая",
            "Игра",
            "Начатая",
            "Серия",
        ),
    )
    save_swiss_stage_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        enabled=True,
        deadline_at="2030-01-02T12:00:00Z",
        direct_qualifier_count=2,
        elimination_qualifier_count=2,
        audit_actor=AUDIT_ACTOR,
    )
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name=None,
        username="anna",
        enabled=True,
        deadline_at="2030-01-03T12:00:00Z",
        points=5,
        now_utc=NOW,
        audit_actor=AUDIT_ACTOR,
    )
    match_ids = []
    for index, (home_index, away_index, starts_at) in enumerate(
        (
            (0, 1, "2030-01-04T15:30:00Z"),
            (2, 3, "2030-01-05T18:00:00Z"),
            (4, 5, "2029-12-31T18:00:00Z"),
            (6, 7, "2030-01-06T18:00:00Z"),
            (8, 9, "2030-01-07T18:00:00Z"),
        ),
        start=1,
    ):
        match_ids.append(
            create_match(
                database_path=database_path,
                telegram_chat_id=CHAT_ID,
                contest_id=contest.id,
                telegram_user_id=USER_ID,
                first_name="Анна",
                last_name=None,
                username="anna",
                home_team_id=team_ids[home_index],
                away_team_id=team_ids[away_index],
                starts_at_utc=starts_at,
                idempotency_key=f"prediction-reminder-match-{index}",
                audit_actor=AUDIT_ACTOR,
            ).match.id
        )
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE matches SET status = 'cancelled' WHERE id = ?",
            (match_ids[-2],),
        )
        connection.execute(
            "UPDATE matches SET status = 'started' WHERE id = ?",
            (match_ids[-1],),
        )
    return contest.id
