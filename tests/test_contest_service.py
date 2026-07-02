from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.contest_service import (
    ContestCreationConflictError,
    ContestNotFoundError,
    MatchCreationConflictError,
    PredictionUnavailableError,
    create_match,
    create_world_cup_2026_contest,
    get_active_contests,
    get_contest_details,
    save_match_prediction,
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


def create_test_match(
    *,
    database_path: Path,
    contest_id: int,
    home_team_name: str = "Аргентина",
    away_team_name: str = "Бразилия",
    starts_at_utc: str = "2026-06-11T18:00:00Z",
    idempotency_key: str = "create-match-1",
):
    return create_match(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        home_team_name=home_team_name,
        away_team_name=away_team_name,
        starts_at_utc=starts_at_utc,
        idempotency_key=idempotency_key,
    )


def save_test_prediction(
    *,
    database_path: Path,
    contest_id: int,
    match_id: int,
    predicted_home_score: int = 2,
    predicted_away_score: int = 1,
    now_utc: datetime = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
):
    return save_match_prediction(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest_id,
        match_id=match_id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        predicted_home_score=predicted_home_score,
        predicted_away_score=predicted_away_score,
        now_utc=now_utc,
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


def test_create_match_creates_teams_stage_event_and_request(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest

    result = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
    )

    assert result.was_created is True
    assert result.match.id == 1
    assert result.match.home_team_name == "Аргентина"
    assert result.match.away_team_name == "Бразилия"
    assert result.match.starts_at_utc == "2026-06-11T18:00:00Z"
    assert result.match.status == "scheduled"

    contest_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
    )
    assert contest_details.id == contest.id
    assert contest_details.name == "ЧМ-2026: прогнозы"
    assert contest_details.matches == (result.match,)

    with create_connection(database_path) as connection:
        stage = connection.execute(
            """
            SELECT name, position, stage_type
            FROM stages
            """
        ).fetchone()
        teams = connection.execute(
            """
            SELECT name
            FROM teams
            ORDER BY id ASC
            """
        ).fetchall()
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
            WHERE event_type = 'match.created'
            """
        ).fetchone()
        request = connection.execute(
            """
            SELECT
                contest_id,
                actor_user_id,
                idempotency_key,
                request_fingerprint,
                match_id
            FROM match_creation_requests
            """
        ).fetchone()

    assert dict(stage) == {
        "name": "Основной этап",
        "position": 1,
        "stage_type": "other",
    }
    assert [team["name"] for team in teams] == ["Аргентина", "Бразилия"]
    assert dict(event) == {
        "contest_id": contest.id,
        "actor_user_id": 1,
        "event_type": "match.created",
        "entity_type": "match",
        "entity_id": result.match.id,
        "payload_json": event["payload_json"],
    }
    assert json.loads(event["payload_json"]) == {
        "away_team": {
            "id": 2,
            "name": "Бразилия",
            "was_created": True,
        },
        "home_team": {
            "id": 1,
            "name": "Аргентина",
            "was_created": True,
        },
        "scoring_rule_set_id": 1,
        "stage": {
            "id": 1,
            "name": "Основной этап",
            "type": "other",
        },
        "starts_at_utc": "2026-06-11T18:00:00Z",
    }
    assert dict(request) == {
        "contest_id": contest.id,
        "actor_user_id": 1,
        "idempotency_key": "create-match-1",
        "request_fingerprint": request["request_fingerprint"],
        "match_id": result.match.id,
    }
    assert request["request_fingerprint"]


def test_create_match_reuses_result_for_same_idempotency_key(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest

    first_result = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        idempotency_key="same-request",
    )
    second_result = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        idempotency_key="same-request",
    )

    assert first_result.was_created is True
    assert second_result.was_created is False
    assert second_result.match == first_result.match

    with create_connection(database_path) as connection:
        matches_count = connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        events_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM event_log
            WHERE event_type = 'match.created'
            """
        ).fetchone()[0]
        requests_count = connection.execute(
            "SELECT COUNT(*) FROM match_creation_requests"
        ).fetchone()[0]

    assert matches_count == 1
    assert events_count == 1
    assert requests_count == 1


def test_create_match_rejects_reused_key_with_other_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest

    create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        idempotency_key="same-request",
    )

    with pytest.raises(
        MatchCreationConflictError,
        match="уже использован с другими данными",
    ):
        create_test_match(
            database_path=database_path,
            contest_id=contest.id,
            starts_at_utc="2026-06-12T18:00:00Z",
            idempotency_key="same-request",
        )

    with create_connection(database_path) as connection:
        matches_count = connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    assert matches_count == 1


def test_get_contest_details_rejects_contest_from_other_chat(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest

    with pytest.raises(ContestNotFoundError, match="Конкурс не найден"):
        get_contest_details(
            database_path=database_path,
            telegram_chat_id=-1009876543210,
            contest_id=contest.id,
        )


def test_create_match_reuses_team_regardless_of_letter_case(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest

    first_result = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        home_team_name="Аргентина",
        away_team_name="Бразилия",
        idempotency_key="first-match",
    )
    second_result = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        home_team_name="аргентина",
        away_team_name="Франция",
        starts_at_utc="2026-06-12T18:00:00Z",
        idempotency_key="second-match",
    )

    assert first_result.was_created is True
    assert second_result.was_created is True

    with create_connection(database_path) as connection:
        teams = connection.execute(
            """
            SELECT id, name
            FROM teams
            ORDER BY id ASC
            """
        ).fetchall()
        match_rows = connection.execute(
            """
            SELECT id, home_team_id, away_team_id
            FROM matches
            ORDER BY id ASC
            """
        ).fetchall()

    assert [dict(team) for team in teams] == [
        {"id": 1, "name": "Аргентина"},
        {"id": 2, "name": "Бразилия"},
        {"id": 3, "name": "Франция"},
    ]
    assert [dict(match) for match in match_rows] == [
        {
            "id": first_result.match.id,
            "home_team_id": 1,
            "away_team_id": 2,
        },
        {
            "id": second_result.match.id,
            "home_team_id": 1,
            "away_team_id": 3,
        },
    ]


def test_save_match_prediction_creates_updates_and_does_not_write_event(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
    ).match

    with create_connection(database_path) as connection:
        events_count_before = connection.execute(
            "SELECT COUNT(*) FROM event_log"
        ).fetchone()[0]

    first_result = save_test_prediction(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        predicted_home_score=2,
        predicted_away_score=1,
    )
    second_result = save_test_prediction(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        predicted_home_score=3,
        predicted_away_score=1,
    )

    assert first_result.was_created is True
    assert first_result.prediction.home_score == 2
    assert first_result.prediction.away_score == 1
    assert second_result.was_created is False
    assert second_result.prediction.home_score == 3
    assert second_result.prediction.away_score == 1

    contest_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
    )
    assert contest_details.matches[0].prediction is not None
    assert contest_details.matches[0].prediction.home_score == 3
    assert contest_details.matches[0].prediction.away_score == 1

    with create_connection(database_path) as connection:
        predictions = connection.execute(
            """
            SELECT
                predicted_home_score,
                predicted_away_score
            FROM match_predictions
            """
        ).fetchall()
        events_count_after = connection.execute(
            "SELECT COUNT(*) FROM event_log"
        ).fetchone()[0]

    assert [dict(prediction) for prediction in predictions] == [
        {
            "predicted_home_score": 3,
            "predicted_away_score": 1,
        }
    ]
    assert events_count_after == events_count_before


def test_get_contest_details_returns_only_current_users_prediction(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
    ).match

    save_test_prediction(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        predicted_home_score=2,
        predicted_away_score=1,
    )
    save_match_prediction(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        match_id=match.id,
        telegram_user_id=456,
        first_name="Second",
        last_name=None,
        username="second-user",
        predicted_home_score=0,
        predicted_away_score=0,
        now_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
    )

    primary_user_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
    )
    second_user_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=456,
    )

    primary_prediction = primary_user_details.matches[0].prediction
    second_prediction = second_user_details.matches[0].prediction

    assert primary_prediction is not None
    assert primary_prediction.home_score == 2
    assert primary_prediction.away_score == 1
    assert second_prediction is not None
    assert second_prediction.home_score == 0
    assert second_prediction.away_score == 0


def test_save_match_prediction_rejects_match_at_or_after_start(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
    ).match

    with pytest.raises(
        PredictionUnavailableError,
        match="Прогнозы на этот матч уже закрыты",
    ):
        save_test_prediction(
            database_path=database_path,
            contest_id=contest.id,
            match_id=match.id,
            now_utc=datetime(2026, 6, 11, 18, 0, tzinfo=timezone.utc),
        )

    with create_connection(database_path) as connection:
        predictions_count = connection.execute(
            "SELECT COUNT(*) FROM match_predictions"
        ).fetchone()[0]

    assert predictions_count == 0
