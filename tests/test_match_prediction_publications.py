from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.database import database_connection, initialize_database
from app.match_prediction_publications import publish_due_match_predictions


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int


class RecordingBot:
    def __init__(self, *, fail_on_call_number: int | None = None) -> None:
        self.fail_on_call_number = fail_on_call_number
        self.calls: list[dict[str, object]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str,
    ) -> SentMessage:
        call_number = len(self.calls) + 1
        if self.fail_on_call_number == call_number:
            raise RuntimeError("Telegram is temporarily unavailable.")
        self.calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        )
        return SentMessage(message_id=1000 + call_number)


def test_disabled_contest_does_not_publish_predictions(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    now_utc = _datetime(12, 0)
    _seed_match(
        database_path=database_path,
        starts_at_utc=now_utc - timedelta(minutes=1),
        participants=(("Анна", "Иванова", 1, 1, "Франция"),),
    )
    bot = RecordingBot()

    asyncio.run(
        publish_due_match_predictions(
            bot=bot,
            database_path=database_path,
            now_utc=now_utc,
        )
    )

    assert bot.calls == []


def test_enabled_contest_does_not_publish_matches_started_before_enabling(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    activation_time = _datetime(12, 0)
    _seed_match(
        database_path=database_path,
        starts_at_utc=activation_time - timedelta(minutes=1),
        publication_enabled_at=activation_time,
        participants=(("Анна", "Иванова", 1, 1, "Франция"),),
    )
    bot = RecordingBot()

    asyncio.run(
        publish_due_match_predictions(
            bot=bot,
            database_path=database_path,
            now_utc=activation_time,
        )
    )

    assert bot.calls == []


def test_enabled_contest_does_not_publish_match_starting_at_enabling_time(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    activation_time = _datetime(12, 0)
    _seed_match(
        database_path=database_path,
        starts_at_utc=activation_time,
        publication_enabled_at=activation_time,
        participants=(("Анна", "Иванова", 1, 1, "Франция"),),
    )
    bot = RecordingBot()

    asyncio.run(
        publish_due_match_predictions(
            bot=bot,
            database_path=database_path,
            now_utc=activation_time,
        )
    )

    assert bot.calls == []


def test_due_match_publication_shows_predictions_and_is_not_repeated(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    activation_time = _datetime(12, 0)
    match_id = _seed_match(
        database_path=database_path,
        starts_at_utc=_datetime(12, 1),
        publication_enabled_at=activation_time,
        participants=(
            ("Анна", "Иванова", 1, 1, "Франция"),
            ("Борис", None, 2, 0, "Франция"),
            ("Илья <&>", "Тест", 0, 0, "Парагвай & Франция"),
        ),
        user_without_prediction=("Без", "Прогноза"),
    )
    bot = RecordingBot()

    asyncio.run(
        publish_due_match_predictions(
            bot=bot,
            database_path=database_path,
            now_utc=activation_time,
        )
    )
    asyncio.run(
        publish_due_match_predictions(
            bot=bot,
            database_path=database_path,
            now_utc=_datetime(12, 2),
        )
    )
    asyncio.run(
        publish_due_match_predictions(
            bot=bot,
            database_path=database_path,
            now_utc=_datetime(12, 3),
        )
    )

    assert len(bot.calls) == 1
    assert bot.calls[0]["chat_id"] == -1001234567890
    assert bot.calls[0]["parse_mode"] == "HTML"
    published_text = str(bot.calls[0]["text"])
    assert "⚽ <b>Парагвай — Франция</b>" in published_text
    assert "• Анна Иванова — 1:1, проходит Франция" in published_text
    assert "• Борис — 2:0" in published_text
    assert (
        "• Илья &lt;&amp;&gt; Тест — 0:0, проходит Парагвай &amp; Франция"
        in published_text
    )
    assert "Без Прогноза" not in published_text

    with database_connection(database_path) as connection:
        completed_row = connection.execute(
            """
            SELECT match_id
            FROM match_prediction_publications
            WHERE match_id = ?
            """,
            (match_id,),
        ).fetchone()
        sent_rows = connection.execute(
            """
            SELECT part_number, telegram_message_id
            FROM match_prediction_publication_messages
            WHERE match_id = ?
            ORDER BY part_number
            """,
            (match_id,),
        ).fetchall()
    assert completed_row is not None
    assert [(row["part_number"], row["telegram_message_id"]) for row in sent_rows] == [
        (0, 1001)
    ]


def test_retry_continues_from_the_first_unsent_message_part(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    activation_time = _datetime(12, 0)
    match_id = _seed_match(
        database_path=database_path,
        starts_at_utc=_datetime(12, 1),
        publication_enabled_at=activation_time,
        participants=tuple(
            (
                f"Участник{number:02d}" * 4,
                f"Фамилия{number:02d}" * 4,
                1,
                0,
                "Франция",
            )
            for number in range(1, 7)
        ),
    )

    asyncio.run(
        publish_due_match_predictions(
            bot=RecordingBot(),
            database_path=database_path,
            now_utc=activation_time,
        )
    )

    failing_bot = RecordingBot(fail_on_call_number=2)
    asyncio.run(
        publish_due_match_predictions(
            bot=failing_bot,
            database_path=database_path,
            now_utc=_datetime(12, 2),
            max_message_length=260,
        )
    )
    assert len(failing_bot.calls) == 1

    with database_connection(database_path) as connection:
        first_attempt_rows = connection.execute(
            """
            SELECT part_number
            FROM match_prediction_publication_messages
            WHERE match_id = ?
            ORDER BY part_number
            """,
            (match_id,),
        ).fetchall()
        completed_before_retry = connection.execute(
            """
            SELECT 1
            FROM match_prediction_publications
            WHERE match_id = ?
            """,
            (match_id,),
        ).fetchone()
    assert [row["part_number"] for row in first_attempt_rows] == [0]
    assert completed_before_retry is None

    retry_bot = RecordingBot()
    asyncio.run(
        publish_due_match_predictions(
            bot=retry_bot,
            database_path=database_path,
            now_utc=_datetime(12, 3),
            max_message_length=260,
        )
    )
    assert retry_bot.calls

    with database_connection(database_path) as connection:
        sent_part_numbers = connection.execute(
            """
            SELECT part_number
            FROM match_prediction_publication_messages
            WHERE match_id = ?
            ORDER BY part_number
            """,
            (match_id,),
        ).fetchall()
        completed_after_retry = connection.execute(
            """
            SELECT 1
            FROM match_prediction_publications
            WHERE match_id = ?
            """,
            (match_id,),
        ).fetchone()
    assert [row["part_number"] for row in sent_part_numbers] == list(
        range(len(sent_part_numbers))
    )
    assert len(sent_part_numbers) == len(retry_bot.calls) + 1
    assert completed_after_retry is not None


def _seed_match(
    *,
    database_path: Path,
    starts_at_utc: datetime,
    participants: tuple[tuple[str, str | None, int, int, str], ...],
    publication_enabled_at: datetime | None = None,
    user_without_prediction: tuple[str, str | None] | None = None,
) -> int:
    initialize_database(database_path)
    with database_connection(database_path) as connection:
        chat_id = connection.execute(
            """
            INSERT INTO chats (telegram_chat_id, title)
            VALUES (?, ?)
            """,
            (-1001234567890, "Футбольный чат"),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO teams (name)
            VALUES ('Парагвай'), ('Франция'), ('Парагвай & Франция')
            """
        )
        paraguay_id = connection.execute(
            "SELECT id FROM teams WHERE name = 'Парагвай'"
        ).fetchone()["id"]
        france_id = connection.execute(
            "SELECT id FROM teams WHERE name = 'Франция'"
        ).fetchone()["id"]
        contest_id = connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug)
            VALUES (?, ?, ?)
            """,
            (chat_id, "ЧМ-2026", "world-cup-2026"),
        ).lastrowid
        if publication_enabled_at is not None:
            connection.execute(
                """
                UPDATE contests
                SET
                    match_prediction_publication_enabled = 1,
                    match_prediction_publication_enabled_at = ?
                WHERE id = ?
                """,
                (publication_enabled_at.isoformat(), contest_id),
            )
        competition_id = connection.execute(
            """
            INSERT INTO competitions (contest_id, name, season, competition_type)
            VALUES (?, ?, ?, ?)
            """,
            (contest_id, "Чемпионат мира", "2026", "world_cup"),
        ).lastrowid
        scoring_rule_set_id = connection.execute(
            """
            INSERT INTO scoring_rule_sets (competition_id, version)
            VALUES (?, 1)
            """,
            (competition_id,),
        ).lastrowid
        stage_id = connection.execute(
            """
            INSERT INTO stages (competition_id, name, position, stage_type)
            VALUES (?, ?, 1, ?)
            """,
            (competition_id, "1/8 финала", "knockout"),
        ).lastrowid
        tie_id = connection.execute(
            """
            INSERT INTO ties (stage_id, scoring_rule_set_id, name, position)
            VALUES (?, ?, ?, 1)
            """,
            (stage_id, scoring_rule_set_id, "Парагвай — Франция"),
        ).lastrowid
        match_id = connection.execute(
            """
            INSERT INTO matches (
                stage_id,
                tie_id,
                scoring_rule_set_id,
                home_team_id,
                away_team_id,
                starts_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                stage_id,
                tie_id,
                scoring_rule_set_id,
                paraguay_id,
                france_id,
                starts_at_utc.isoformat(),
            ),
        ).lastrowid

        for number, participant in enumerate(participants, start=1):
            (
                first_name,
                last_name,
                home_score,
                away_score,
                advancing_team_name,
            ) = participant
            user_id = connection.execute(
                """
                INSERT INTO users (telegram_user_id, first_name, last_name)
                VALUES (?, ?, ?)
                """,
                (1000 + number, first_name, last_name),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO match_predictions (
                    match_id,
                    user_id,
                    predicted_home_score,
                    predicted_away_score
                )
                VALUES (?, ?, ?, ?)
                """,
                (match_id, user_id, home_score, away_score),
            )
            advancing_team_id = connection.execute(
                "SELECT id FROM teams WHERE name = ?",
                (advancing_team_name,),
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO tie_predictions (
                    tie_id,
                    user_id,
                    predicted_advancing_team_id
                )
                VALUES (?, ?, ?)
                """,
                (tie_id, user_id, advancing_team_id),
            )

        if user_without_prediction is not None:
            first_name, last_name = user_without_prediction
            connection.execute(
                """
                INSERT INTO users (telegram_user_id, first_name, last_name)
                VALUES (?, ?, ?)
                """,
                (9999, first_name, last_name),
            )
    return int(match_id)


def _datetime(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 7, hour, minute, tzinfo=timezone.utc)
