from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contest_service import (
    ContestCreationConflictError,
    create_world_cup_2026_contest,
    get_active_contests,
)
from app.database import create_connection, initialize_database


TELEGRAM_CHAT_ID = -1001234567890
TELEGRAM_USER_ID = 123


def create_contest(
    *,
    database_path: Path,
    contest_name: str = "ЧМ-2026: прогнозы",
    idempotency_key: str = "create-contest-1",
):
    return create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        chat_title="Футбольные прогнозы",
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name=contest_name,
        idempotency_key=idempotency_key,
    )


def test_get_active_contests_returns_empty_tuple_without_contests(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    contests = get_active_contests(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
    )

    assert contests == ()

    with create_connection(database_path) as connection:
        chats_count = connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0]

    assert chats_count == 0


def test_get_active_contests_returns_all_active_contests_and_excludes_archived(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        chat_id = int(
            connection.execute(
                """
                INSERT INTO chats (telegram_chat_id, title)
                VALUES (?, ?)
                """,
                (TELEGRAM_CHAT_ID, "Футбольные прогнозы"),
            ).lastrowid
        )

        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, "Чемпионат мира 2026", "world-cup-2026", 1),
        )
        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, "Лига чемпионов 2026/27", "champions-league-2026-27", 1),
        )
        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, "Архивный конкурс", "archived-contest", 0),
        )

    contests = get_active_contests(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
    )

    assert [(contest.name, contest.slug) for contest in contests] == [
        ("Лига чемпионов 2026/27", "champions-league-2026-27"),
        ("Чемпионат мира 2026", "world-cup-2026"),
    ]
    assert all(contest.created_at for contest in contests)


def test_create_world_cup_2026_contest_creates_related_entities_and_event_log(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    result = create_contest(database_path=database_path)

    assert result.was_created is True
    assert result.contest.name == "ЧМ-2026: прогнозы"
    assert result.contest.slug.startswith("world-cup-2026-")
    assert result.contest.created_at

    contests = get_active_contests(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
    )
    assert contests == (result.contest,)

    with create_connection(database_path) as connection:
        chat = connection.execute(
            """
            SELECT telegram_chat_id, title
            FROM chats
            """
        ).fetchone()
        user = connection.execute(
            """
            SELECT telegram_user_id, username, first_name, last_name
            FROM users
            """
        ).fetchone()
        competition = connection.execute(
            """
            SELECT name, season, competition_type, is_active
            FROM competitions
            WHERE contest_id = ?
            """,
            (result.contest.id,),
        ).fetchone()
        scoring_rule_set = connection.execute(
            """
            SELECT
                version,
                exact_score_points,
                goal_difference_points,
                outcome_points,
                advancing_team_points,
                is_active
            FROM scoring_rule_sets
            WHERE competition_id = (
                SELECT id
                FROM competitions
                WHERE contest_id = ?
            )
            """,
            (result.contest.id,),
        ).fetchone()
        event = connection.execute(
            """
            SELECT
                contest_id,
                actor_user_id,
                event_type,
                entity_type,
                entity_id,
                payload_json
            FROM event_log
            """
        ).fetchone()
        request = connection.execute(
            """
            SELECT
                chat_id,
                actor_user_id,
                idempotency_key,
                request_fingerprint,
                contest_id
            FROM contest_creation_requests
            """
        ).fetchone()

    assert dict(chat) == {
        "telegram_chat_id": TELEGRAM_CHAT_ID,
        "title": "Футбольные прогнозы",
    }
    assert dict(user) == {
        "telegram_user_id": TELEGRAM_USER_ID,
        "username": "evsab",
        "first_name": "Eugene",
        "last_name": "Sabir",
    }
    assert dict(competition) == {
        "name": "Чемпионат мира",
        "season": "2026",
        "competition_type": "world_cup",
        "is_active": 1,
    }
    assert dict(scoring_rule_set) == {
        "version": 1,
        "exact_score_points": 3,
        "goal_difference_points": 2,
        "outcome_points": 1,
        "advancing_team_points": 1,
        "is_active": 1,
    }
    assert dict(event) == {
        "contest_id": result.contest.id,
        "actor_user_id": 1,
        "event_type": "contest.created",
        "entity_type": "contest",
        "entity_id": result.contest.id,
        "payload_json": event["payload_json"],
    }

    event_payload = json.loads(event["payload_json"])
    assert event_payload == {
        "competition": {
            "id": 1,
            "name": "Чемпионат мира",
            "season": "2026",
            "type": "world_cup",
        },
        "contest_name": "ЧМ-2026: прогнозы",
        "scoring_rule_set": {
            "advancing_team_points": 1,
            "exact_score_points": 3,
            "goal_difference_points": 2,
            "id": 1,
            "outcome_points": 1,
            "version": 1,
        },
    }
    assert dict(request) == {
        "chat_id": 1,
        "actor_user_id": 1,
        "idempotency_key": "create-contest-1",
        "request_fingerprint": request["request_fingerprint"],
        "contest_id": result.contest.id,
    }
    assert request["request_fingerprint"]


def test_create_world_cup_2026_contest_reuses_result_for_same_idempotency_key(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    first_result = create_contest(
        database_path=database_path,
        idempotency_key="same-request",
    )
    second_result = create_contest(
        database_path=database_path,
        idempotency_key="same-request",
    )

    assert first_result.was_created is True
    assert second_result.was_created is False
    assert second_result.contest == first_result.contest

    with create_connection(database_path) as connection:
        contests_count = connection.execute("SELECT COUNT(*) FROM contests").fetchone()[
            0
        ]
        competitions_count = connection.execute(
            "SELECT COUNT(*) FROM competitions"
        ).fetchone()[0]
        scoring_rule_sets_count = connection.execute(
            "SELECT COUNT(*) FROM scoring_rule_sets"
        ).fetchone()[0]
        events_count = connection.execute("SELECT COUNT(*) FROM event_log").fetchone()[
            0
        ]
        requests_count = connection.execute(
            "SELECT COUNT(*) FROM contest_creation_requests"
        ).fetchone()[0]

    assert contests_count == 1
    assert competitions_count == 1
    assert scoring_rule_sets_count == 1
    assert events_count == 1
    assert requests_count == 1


def test_create_world_cup_2026_contest_rejects_reused_key_with_other_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    create_contest(
        database_path=database_path,
        contest_name="Первый конкурс",
        idempotency_key="same-request",
    )

    with pytest.raises(
        ContestCreationConflictError,
        match="уже использован с другими данными",
    ):
        create_contest(
            database_path=database_path,
            contest_name="Другой конкурс",
            idempotency_key="same-request",
        )

    with create_connection(database_path) as connection:
        contests_count = connection.execute("SELECT COUNT(*) FROM contests").fetchone()[
            0
        ]

    assert contests_count == 1


def test_create_world_cup_2026_contest_allows_parallel_contests_in_one_chat(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    first_result = create_contest(
        database_path=database_path,
        contest_name="Основной конкурс",
        idempotency_key="first-request",
    )
    second_result = create_contest(
        database_path=database_path,
        contest_name="Конкурс для друзей",
        idempotency_key="second-request",
    )

    assert first_result.was_created is True
    assert second_result.was_created is True
    assert first_result.contest.id != second_result.contest.id

    contests = get_active_contests(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
    )

    assert {contest.name for contest in contests} == {
        "Основной конкурс",
        "Конкурс для друзей",
    }


@pytest.mark.parametrize(
    "contest_name, error_message",
    [
        ("", "Введите название конкурса."),
        ("   ", "Введите название конкурса."),
        ("x" * 81, "Название конкурса не должно быть длиннее 80 символов."),
    ],
)
def test_create_world_cup_2026_contest_validates_name_before_writes(
    tmp_path: Path,
    contest_name: str,
    error_message: str,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with pytest.raises(ValueError, match=error_message):
        create_contest(
            database_path=database_path,
            contest_name=contest_name,
        )

    with create_connection(database_path) as connection:
        chats_count = connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        users_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        contests_count = connection.execute("SELECT COUNT(*) FROM contests").fetchone()[
            0
        ]

    assert chats_count == 0
    assert users_count == 0
    assert contests_count == 0
