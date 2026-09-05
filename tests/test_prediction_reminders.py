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
    save_match_prediction,
    save_swiss_stage_prediction_settings,
    save_tournament_teams,
)
from app.database import database_connection, initialize_database
from app.prediction_reminders import (
    AutomaticReminderDeadline,
    AutomaticReminderMatch,
    NoOpenPredictionRemindersError,
    PredictionReminderMessageTooLongError,
    ReminderMentionRecipient,
    build_automatic_prediction_reminder_message,
    build_prediction_reminder_message,
    publish_prediction_reminders,
)
from app.user_service import upsert_chat_actor
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
    assert "Прогноз на общий этап" in message.html
    assert "Не забудьте сделать или дозаполнить прогнозы" in message.html
    assert "швейцарский этап" not in message.html.lower()

    automatic = build_automatic_prediction_reminder_message(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        contest_name=contest.name,
        matches=(),
        deadlines=(
            AutomaticReminderDeadline(
                kind="swiss",
                deadline_at_utc=datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc),
            ),
        ),
        eligible_recipients=(),
        now_utc=NOW,
    )
    assert automatic.reminder_count == 1
    assert automatic.match_count == 0
    assert "Прогноз на общий этап" in automatic.html


def test_automatic_tournament_reminder_is_scoped_to_supplied_deadline_batch(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "automatic-deadline-scope.db"
    contest_id = _configured_contest(database_path)

    message = build_automatic_prediction_reminder_message(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        contest_name="Кубок",
        matches=(),
        deadlines=(
            AutomaticReminderDeadline(
                kind="swiss",
                deadline_at_utc=datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc),
            ),
        ),
        eligible_recipients=(),
        now_utc=NOW,
    )

    assert "Прогноз на швейцарский этап" in message.html
    assert "Прогноз на чемпиона" not in message.html
    assert message.reminder_count == 1
    assert message.match_count == 0


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


def test_automatic_batch_is_explicit_and_mentions_only_missing_recipient(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _configured_contest(database_path)
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT matches.id, matches.starts_at_utc, matches.home_team_id,
                   home.name AS home_name, away.name AS away_name
            FROM matches
            JOIN teams AS home ON home.id = matches.home_team_id
            JOIN teams AS away ON away.id = matches.away_team_id
            JOIN stages ON stages.id = matches.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ? AND matches.status = 'scheduled'
              AND matches.starts_at_utc > ?
            ORDER BY matches.starts_at_utc, matches.id
            """,
            (contest_id, NOW.isoformat()),
        ).fetchall()
    first, selected = rows
    second_actor = upsert_chat_actor(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=222,
        username=None,
        first_name="Боб <&",
        last_name="Тест",
    )
    creator_actor = upsert_chat_actor(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=USER_ID,
        username="anna",
        first_name="Анна",
        last_name=None,
    )
    save_match_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        match_id=int(first["id"]),
        telegram_user_id=222,
        first_name="Боб <&",
        last_name="Тест",
        username=None,
        predicted_home_score=1,
        predicted_away_score=0,
        predicted_advancing_team_id=int(first["home_team_id"]),
        now_utc=NOW,
    )
    save_match_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        match_id=int(selected["id"]),
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name=None,
        username="anna",
        predicted_home_score=1,
        predicted_away_score=0,
        predicted_advancing_team_id=int(selected["home_team_id"]),
        now_utc=NOW,
    )
    eligible = (
        ReminderMentionRecipient(
            user_id=creator_actor.actor_user_id,
            telegram_user_id=USER_ID,
            first_name="Анна",
            last_name=None,
        ),
        ReminderMentionRecipient(
            user_id=second_actor.actor_user_id,
            telegram_user_id=222,
            first_name="Боб <&",
            last_name="Тест",
        ),
    )

    message = build_automatic_prediction_reminder_message(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        contest_name="Кубок",
        matches=(
            AutomaticReminderMatch(
                match_id=int(selected["id"]),
                starts_at_utc=datetime.fromisoformat(
                    str(selected["starts_at_utc"]).replace("Z", "+00:00")
                ),
                home_team_name=str(selected["home_name"]),
                away_team_name=str(selected["away_name"]),
            ),
        ),
        eligible_recipients=eligible,
        now_utc=NOW,
    )

    assert str(selected["home_name"]) in message.html
    assert str(first["home_name"]) not in message.html
    assert 'href="tg://user?id=222"' in message.html
    assert "Боб &lt;&amp; Тест" in message.html
    assert f"tg://user?id={USER_ID}" not in message.html
    assert message.mention_count == 1

    complete_only = build_automatic_prediction_reminder_message(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        contest_name="Кубок",
        matches=(
            AutomaticReminderMatch(
                match_id=int(selected["id"]),
                starts_at_utc=datetime.fromisoformat(
                    str(selected["starts_at_utc"]).replace("Z", "+00:00")
                ),
                home_team_name=str(selected["home_name"]),
                away_team_name=str(selected["away_name"]),
            ),
        ),
        eligible_recipients=(eligible[0],),
        now_utc=NOW,
    )
    assert complete_only.mention_count == 0
    assert "Ждём прогнозы от" not in complete_only.html
    assert complete_only.match_count == 1


def test_automatic_mentions_split_deterministically_without_repeating_tags(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _configured_contest(database_path)
    with database_connection(database_path) as connection:
        match = connection.execute(
            """
            SELECT matches.id, matches.starts_at_utc,
                   home.name AS home_name, away.name AS away_name
            FROM matches
            JOIN teams AS home ON home.id = matches.home_team_id
            JOIN teams AS away ON away.id = matches.away_team_id
            JOIN stages ON stages.id = matches.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ? AND matches.status = 'scheduled'
              AND matches.starts_at_utc > ?
            ORDER BY matches.starts_at_utc, matches.id
            LIMIT 1
            """,
            (contest_id, NOW.isoformat()),
        ).fetchone()
    recipients = tuple(
        ReminderMentionRecipient(
            user_id=10_000 + index,
            telegram_user_id=20_000 + index,
            first_name=f"Участник {index:03d} " + "Я" * 24,
            last_name=None,
        )
        for index in range(80)
    )
    kwargs = {
        "database_path": database_path,
        "telegram_chat_id": CHAT_ID,
        "contest_id": contest_id,
        "contest_name": "Кубок",
        "matches": (
            AutomaticReminderMatch(
                match_id=int(match["id"]),
                starts_at_utc=datetime.fromisoformat(
                    str(match["starts_at_utc"]).replace("Z", "+00:00")
                ),
                home_team_name=str(match["home_name"]),
                away_team_name=str(match["away_name"]),
            ),
        ),
        "eligible_recipients": recipients,
        "now_utc": NOW,
        "max_message_length": 500,
    }

    first_render = build_automatic_prediction_reminder_message(**kwargs)
    second_render = build_automatic_prediction_reminder_message(**kwargs)

    assert first_render.html_parts == second_render.html_parts
    assert len(first_render.html_parts) > 1
    assert all(len(part) <= 500 for part in first_render.html_parts)
    combined = "".join(first_render.html_parts)
    for recipient in recipients:
        assert combined.count(f"tg://user?id={recipient.telegram_user_id}") == 1


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
