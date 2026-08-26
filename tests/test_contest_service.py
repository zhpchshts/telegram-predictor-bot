from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue
from threading import Event, local

import pytest

from app import contest_service
from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    ContestCreationConflictError,
    ContestNotFoundError,
    MatchCreationConflictError,
    MatchNotFoundError,
    MatchResultUnavailableError,
    MatchUpdateUnavailableError,
    PredictionUnavailableError,
    create_match,
    create_the_international_2026_contest,
    delete_match,
    create_world_cup_2026_contest,
    delete_contest,
    get_active_contests,
    get_contest_details,
    save_match_prediction,
    save_match_result,
    update_match_start,
    save_match_prediction_publication_settings,
    ContestCompletedError,
    ContestCompletionUnavailableError,
    complete_contest,
    get_completed_contests,
)
from app.database import create_connection, initialize_database
from tests.support import ensure_contest_teams


TELEGRAM_CHAT_ID = -1001234567890
TELEGRAM_USER_ID = 123
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=TELEGRAM_CHAT_ID,
    telegram_user_id=TELEGRAM_USER_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


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
        audit_actor=AUDIT_ACTOR,
    )


def create_ti_contest(
    *,
    database_path: Path,
    contest_name: str = "The International 2026",
    idempotency_key: str = "create-ti-contest-1",
):
    return create_the_international_2026_contest(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        chat_title="Dota 2 predictions",
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name=contest_name,
        idempotency_key=idempotency_key,
        audit_actor=AUDIT_ACTOR,
    )


def create_test_match(
    *,
    database_path: Path,
    contest_id: int,
    home_team_name: str = "Аргентина",
    away_team_name: str = "Бразилия",
    starts_at_utc: str = "2026-06-11T18:00:00Z",
    best_of: int | None = None,
    idempotency_key: str = "create-match-1",
):
    home_team_id, away_team_id = ensure_contest_teams(
        database_path,
        contest_id=contest_id,
        names=(home_team_name, away_team_name),
    )
    return create_match(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        starts_at_utc=starts_at_utc,
        best_of=best_of,
        idempotency_key=idempotency_key,
        audit_actor=AUDIT_ACTOR,
    )


def delete_test_match(
    *,
    database_path: Path,
    contest_id: int,
    match_id: int,
) -> None:
    delete_match(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest_id,
        match_id=match_id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        audit_actor=AUDIT_ACTOR,
    )


def update_test_match_start(
    *,
    database_path: Path,
    contest_id: int,
    match_id: int,
    starts_at_utc: str,
    now_utc: datetime,
):
    return update_match_start(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest_id,
        match_id=match_id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        starts_at_utc=starts_at_utc,
        audit_actor=AUDIT_ACTOR,
        now_utc=now_utc,
    )


def save_test_prediction(
    *,
    database_path: Path,
    contest_id: int,
    match_id: int,
    predicted_home_score: int = 2,
    predicted_away_score: int = 1,
    predicted_advancing_team_id: int | None = 1,
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
        predicted_advancing_team_id=predicted_advancing_team_id,
        now_utc=now_utc,
    )


def save_test_result(
    *,
    database_path: Path,
    contest_id: int,
    match_id: int,
    home_score: int = 2,
    away_score: int = 1,
    advancing_team_id: int | None = 1,
    now_utc: datetime = datetime(
        2026,
        6,
        11,
        18,
        0,
        tzinfo=timezone.utc,
    ),
):
    return save_match_result(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest_id,
        match_id=match_id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        home_score=home_score,
        away_score=away_score,
        advancing_team_id=advancing_team_id,
        now_utc=now_utc,
        audit_actor=AUDIT_ACTOR,
    )


def complete_test_contest(
    *,
    database_path: Path,
    contest_id: int,
) -> None:
    complete_contest(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        audit_actor=AUDIT_ACTOR,
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


def test_get_active_contests_returns_all_active_contests_and_excludes_completed(
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
            (chat_id, "Завершённый конкурс", "completed-contest", 0),
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


def test_create_the_international_2026_contest_uses_ti_template_and_rules(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    result = create_ti_contest(database_path=database_path)

    assert result.was_created is True
    assert result.contest.slug.startswith("the-international-2026-")

    with create_connection(database_path) as connection:
        contest = connection.execute(
            """
            SELECT template_key, champion_prediction_points
            FROM contests
            WHERE id = ?
            """,
            (result.contest.id,),
        ).fetchone()
        competition = connection.execute(
            """
            SELECT name, season, competition_type
            FROM competitions
            WHERE contest_id = ?
            """,
            (result.contest.id,),
        ).fetchone()
        scoring_rule_set = connection.execute(
            """
            SELECT
                exact_score_points,
                goal_difference_points,
                outcome_points,
                advancing_team_points
            FROM scoring_rule_sets
            WHERE competition_id = (
                SELECT id
                FROM competitions
                WHERE contest_id = ?
            )
            """,
            (result.contest.id,),
        ).fetchone()

    assert dict(contest) == {
        "template_key": "the_international_2026",
        "champion_prediction_points": 4,
    }
    assert dict(competition) == {
        "name": "The International",
        "season": "2026",
        "competition_type": "the_international",
    }
    assert dict(scoring_rule_set) == {
        "exact_score_points": 2,
        "goal_difference_points": 0,
        "outcome_points": 1,
        "advancing_team_points": 0,
    }


def test_contest_creation_idempotency_distinguishes_templates(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    create_contest(
        database_path=database_path,
        contest_name="Predictions",
        idempotency_key="same-request",
    )

    with pytest.raises(ContestCreationConflictError):
        create_ti_contest(
            database_path=database_path,
            contest_name="Predictions",
            idempotency_key="same-request",
        )


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
    assert result.match.tie_id == 1
    assert result.match.home_team_id == 1
    assert result.match.away_team_id == 2
    assert result.match.home_team_name == "Аргентина"
    assert result.match.away_team_name == "Бразилия"
    assert result.match.starts_at_utc == "2026-06-11T18:00:00Z"
    assert result.match.status == "scheduled"

    contest_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        now_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
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
        tie = connection.execute(
            """
            SELECT
                id,
                stage_id,
                scoring_rule_set_id,
                name,
                position,
                is_two_legged,
                advancing_team_id
            FROM ties
            """
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
        "name": "Плей-офф",
        "position": 1,
        "stage_type": "knockout",
    }
    assert [team["name"] for team in teams] == ["Аргентина", "Бразилия"]
    assert dict(tie) == {
        "id": result.match.tie_id,
        "stage_id": 1,
        "scoring_rule_set_id": 1,
        "name": "Аргентина — Бразилия",
        "position": 1,
        "is_two_legged": 0,
        "advancing_team_id": None,
    }
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
        },
        "home_team": {
            "id": 1,
            "name": "Аргентина",
        },
        "scoring_rule_set_id": 1,
        "stage": {
            "id": 1,
            "name": "Плей-офф",
            "type": "knockout",
        },
        "tie": {
            "id": 1,
            "is_two_legged": False,
            "name": "Аргентина — Бразилия",
            "position": 1,
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


def test_ti_match_requires_best_of_and_stores_series_format(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_ti_contest(database_path=database_path).contest

    with pytest.raises(ValueError, match="Bo3.*Bo5"):
        create_test_match(database_path=database_path, contest_id=contest.id)

    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        best_of=3,
    ).match

    assert match.best_of == 3


def test_football_match_keeps_best_of_empty(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest

    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
    ).match

    assert match.best_of is None


def test_ti_prediction_derives_winner_and_rejects_incomplete_score(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_ti_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        best_of=3,
    ).match

    prediction = save_test_prediction(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        predicted_home_score=2,
        predicted_away_score=1,
        predicted_advancing_team_id=None,
    ).prediction

    assert prediction.advancing_team_id == match.home_team_id

    with pytest.raises(ValueError, match="Bo3"):
        save_test_prediction(
            database_path=database_path,
            contest_id=contest.id,
            match_id=match.id,
            predicted_home_score=1,
            predicted_away_score=0,
            predicted_advancing_team_id=None,
        )

    result = save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        home_score=1,
        away_score=2,
        advancing_team_id=None,
    ).result

    assert result.advancing_team_id == match.away_team_id


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
        predicted_advancing_team_id=2,
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
    assert primary_prediction.advancing_team_id == 1
    assert second_prediction is not None
    assert second_prediction.home_score == 0
    assert second_prediction.away_score == 0
    assert second_prediction.advancing_team_id == 2


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


def test_save_match_result_creates_corrects_recalculates_scores_and_writes_events(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
    ).match

    def save_prediction_for_user(
        *,
        telegram_user_id: int,
        predicted_home_score: int,
        predicted_away_score: int,
        predicted_advancing_team_id: int,
    ) -> None:
        save_match_prediction(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            contest_id=contest.id,
            match_id=match.id,
            telegram_user_id=telegram_user_id,
            first_name=f"User {telegram_user_id}",
            last_name=None,
            username=f"user-{telegram_user_id}",
            predicted_home_score=predicted_home_score,
            predicted_away_score=predicted_away_score,
            predicted_advancing_team_id=predicted_advancing_team_id,
            now_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        )

    save_prediction_for_user(
        telegram_user_id=123,
        predicted_home_score=2,
        predicted_away_score=1,
        predicted_advancing_team_id=1,
    )
    save_prediction_for_user(
        telegram_user_id=456,
        predicted_home_score=3,
        predicted_away_score=2,
        predicted_advancing_team_id=1,
    )
    save_prediction_for_user(
        telegram_user_id=789,
        predicted_home_score=1,
        predicted_away_score=0,
        predicted_advancing_team_id=1,
    )
    save_prediction_for_user(
        telegram_user_id=101112,
        predicted_home_score=0,
        predicted_away_score=1,
        predicted_advancing_team_id=2,
    )
    save_prediction_for_user(
        telegram_user_id=131415,
        predicted_home_score=0,
        predicted_away_score=0,
        predicted_advancing_team_id=2,
    )

    first_result = save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        home_score=2,
        away_score=1,
        advancing_team_id=1,
    )

    with create_connection(database_path) as connection:
        first_match_scores = connection.execute(
            """
            SELECT
                users.telegram_user_id,
                match_prediction_scores.score_type,
                match_prediction_scores.points
            FROM match_prediction_scores
            JOIN match_predictions
                ON match_predictions.id =
                    match_prediction_scores.match_prediction_id
            JOIN users
                ON users.id = match_predictions.user_id
            ORDER BY users.telegram_user_id ASC
            """
        ).fetchall()
        first_tie_scores = connection.execute(
            """
            SELECT
                users.telegram_user_id,
                tie_prediction_scores.points
            FROM tie_prediction_scores
            JOIN tie_predictions
                ON tie_predictions.id =
                    tie_prediction_scores.tie_prediction_id
            JOIN users
                ON users.id = tie_predictions.user_id
            WHERE tie_predictions.tie_id = ?
            ORDER BY users.telegram_user_id ASC
            """,
            (match.tie_id,),
        ).fetchall()

    assert first_result.was_created is True
    assert first_result.result.home_score == 2
    assert first_result.result.away_score == 1
    assert first_result.result.advancing_team_id == 1
    assert [dict(score) for score in first_match_scores] == [
        {
            "telegram_user_id": 123,
            "score_type": "exact_score",
            "points": 3,
        },
        {
            "telegram_user_id": 456,
            "score_type": "goal_difference",
            "points": 2,
        },
        {
            "telegram_user_id": 789,
            "score_type": "goal_difference",
            "points": 2,
        },
    ]
    assert [dict(score) for score in first_tie_scores] == [
        {
            "telegram_user_id": 123,
            "points": 1,
        },
        {
            "telegram_user_id": 456,
            "points": 1,
        },
        {
            "telegram_user_id": 789,
            "points": 1,
        },
    ]

    second_result = save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        home_score=1,
        away_score=1,
        advancing_team_id=2,
    )

    contest_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
    )
    match_details = contest_details.matches[0]

    with create_connection(database_path) as connection:
        saved_match = connection.execute(
            """
            SELECT
                status,
                home_score_final,
                away_score_final
            FROM matches
            WHERE id = ?
            """,
            (match.id,),
        ).fetchone()
        saved_tie = connection.execute(
            """
            SELECT advancing_team_id
            FROM ties
            WHERE id = ?
            """,
            (match.tie_id,),
        ).fetchone()
        corrected_match_scores = connection.execute(
            """
            SELECT
                users.telegram_user_id,
                match_prediction_scores.score_type,
                match_prediction_scores.points
            FROM match_prediction_scores
            JOIN match_predictions
                ON match_predictions.id =
                    match_prediction_scores.match_prediction_id
            JOIN users
                ON users.id = match_predictions.user_id
            ORDER BY users.telegram_user_id ASC
            """
        ).fetchall()
        corrected_tie_scores = connection.execute(
            """
            SELECT
                users.telegram_user_id,
                tie_prediction_scores.points
            FROM tie_prediction_scores
            JOIN tie_predictions
                ON tie_predictions.id =
                    tie_prediction_scores.tie_prediction_id
            JOIN users
                ON users.id = tie_predictions.user_id
            WHERE tie_predictions.tie_id = ?
            ORDER BY users.telegram_user_id ASC
            """,
            (match.tie_id,),
        ).fetchall()
        events = connection.execute(
            """
            SELECT
                actor_user_id,
                event_type,
                entity_type,
                entity_id,
                payload_json
            FROM event_log
            WHERE event_type IN (
                'match.result_recorded',
                'match.result_corrected'
            )
            ORDER BY id ASC
            """
        ).fetchall()

    assert second_result.was_created is False
    assert second_result.result.home_score == 1
    assert second_result.result.away_score == 1
    assert second_result.result.advancing_team_id == 2

    assert match_details.status == "finished"
    assert match_details.result is not None
    assert match_details.result.home_score == 1
    assert match_details.result.away_score == 1
    assert match_details.result.advancing_team_id == 2

    assert match_details.prediction is not None
    assert match_details.prediction.home_score == 2
    assert match_details.prediction.away_score == 1
    assert match_details.prediction.advancing_team_id == 1

    assert dict(saved_match) == {
        "status": "finished",
        "home_score_final": 1,
        "away_score_final": 1,
    }
    assert dict(saved_tie) == {
        "advancing_team_id": 2,
    }

    assert [dict(score) for score in corrected_match_scores] == [
        {
            "telegram_user_id": 131415,
            "score_type": "goal_difference",
            "points": 2,
        },
    ]
    assert [dict(score) for score in corrected_tie_scores] == [
        {
            "telegram_user_id": 101112,
            "points": 1,
        },
        {
            "telegram_user_id": 131415,
            "points": 1,
        },
    ]

    assert [dict(event) for event in events] == [
        {
            "actor_user_id": 1,
            "event_type": "match.result_recorded",
            "entity_type": "match",
            "entity_id": match.id,
            "payload_json": events[0]["payload_json"],
        },
        {
            "actor_user_id": 1,
            "event_type": "match.result_corrected",
            "entity_type": "match",
            "entity_id": match.id,
            "payload_json": events[1]["payload_json"],
        },
    ]
    assert [json.loads(event["payload_json"]) for event in events] == [
        {
            "previous_result": None,
            "result": {
                "advancing_team_id": 1,
                "away_score": 1,
                "home_score": 2,
            },
        },
        {
            "previous_result": {
                "advancing_team_id": 1,
                "away_score": 1,
                "home_score": 2,
            },
            "result": {
                "advancing_team_id": 2,
                "away_score": 1,
                "home_score": 1,
            },
        },
    ]


def test_save_match_result_exact_retry_is_a_side_effect_free_noop(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "idempotent-result.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        starts_at_utc="2030-06-11T18:00:00Z",
    ).match
    save_match_prediction_publication_settings(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        enabled=True,
        now_utc=datetime(2030, 6, 1, tzinfo=timezone.utc),
        audit_actor=AUDIT_ACTOR,
    )
    first = save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        advancing_team_id=match.home_team_id,
        now_utc=datetime(2030, 6, 11, 18, 0, tzinfo=timezone.utc),
    )

    def result_side_effects() -> tuple[int, int, tuple[object, ...]]:
        with create_connection(database_path) as connection:
            event_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM event_log
                    WHERE contest_id = ?
                      AND event_type IN (
                          'match.result_recorded',
                          'match.result_corrected'
                      )
                    """,
                    (contest.id,),
                ).fetchone()[0]
            )
            audit_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM audit_events
                    WHERE contest_id = ?
                      AND event_type IN ('match_result_set', 'match_result_changed')
                    """,
                    (contest.id,),
                ).fetchone()[0]
            )
            publication = connection.execute(
                """
                SELECT desired_revision, latest_event_id, updated_at
                FROM contest_publications
                WHERE contest_id = ?
                  AND publication_type = 'match_result'
                  AND entity_id = ?
                """,
                (contest.id, match.id),
            ).fetchone()
        assert publication is not None
        return event_count, audit_count, tuple(publication)

    side_effects_before_retry = result_side_effects()
    repeated = save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        advancing_team_id=match.home_team_id,
        now_utc=datetime(2030, 6, 11, 18, 1, tzinfo=timezone.utc),
    )

    assert first.was_created is True
    assert repeated.was_created is False
    assert repeated.result == first.result
    assert result_side_effects() == side_effects_before_retry


def test_save_match_result_repairs_finished_result_with_inconsistent_status(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "inconsistent-result-status.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        starts_at_utc="2030-06-11T18:00:00Z",
    ).match
    first = save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        advancing_team_id=match.home_team_id,
        now_utc=datetime(2030, 6, 11, 18, 0, tzinfo=timezone.utc),
    )
    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE matches SET status = 'started' WHERE id = ?",
            (match.id,),
        )
        correction_count_before = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM event_log
                WHERE contest_id = ? AND event_type = 'match.result_corrected'
                """,
                (contest.id,),
            ).fetchone()[0]
        )

    repaired = save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        advancing_team_id=match.home_team_id,
        now_utc=datetime(2030, 6, 11, 18, 1, tzinfo=timezone.utc),
    )

    with create_connection(database_path) as connection:
        stored_status = str(
            connection.execute(
                "SELECT status FROM matches WHERE id = ?",
                (match.id,),
            ).fetchone()["status"]
        )
        correction_count_after = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM event_log
                WHERE contest_id = ? AND event_type = 'match.result_corrected'
                """,
                (contest.id,),
            ).fetchone()[0]
        )

    assert repaired.was_created is False
    assert repaired.result == first.result
    assert stored_status == "finished"
    assert correction_count_after == correction_count_before + 1


def test_save_match_result_rejects_match_before_start_without_writes(
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

    with pytest.raises(
        MatchResultUnavailableError,
        match="Результат можно внести только после начала матча",
    ):
        save_test_result(
            database_path=database_path,
            contest_id=contest.id,
            match_id=match.id,
            now_utc=datetime(
                2026,
                6,
                11,
                17,
                59,
                59,
                tzinfo=timezone.utc,
            ),
        )

    with create_connection(database_path) as connection:
        saved_match = connection.execute(
            """
            SELECT status, home_score_final, away_score_final
            FROM matches
            WHERE id = ?
            """,
            (match.id,),
        ).fetchone()
        events_count_after = connection.execute(
            "SELECT COUNT(*) FROM event_log"
        ).fetchone()[0]

    assert dict(saved_match) == {
        "status": "scheduled",
        "home_score_final": None,
        "away_score_final": None,
    }
    assert events_count_after == events_count_before


def test_save_match_result_rejects_cancelled_match_without_writes(
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
        connection.execute(
            """
            UPDATE matches
            SET status = 'cancelled'
            WHERE id = ?
            """,
            (match.id,),
        )
        events_count_before = connection.execute(
            "SELECT COUNT(*) FROM event_log"
        ).fetchone()[0]

    with pytest.raises(
        MatchResultUnavailableError,
        match="Для отменённого матча нельзя сохранить результат",
    ):
        save_test_result(
            database_path=database_path,
            contest_id=contest.id,
            match_id=match.id,
        )

    with create_connection(database_path) as connection:
        saved_match = connection.execute(
            """
            SELECT status, home_score_final, away_score_final
            FROM matches
            WHERE id = ?
            """,
            (match.id,),
        ).fetchone()
        events_count_after = connection.execute(
            "SELECT COUNT(*) FROM event_log"
        ).fetchone()[0]

    assert dict(saved_match) == {
        "status": "cancelled",
        "home_score_final": None,
        "away_score_final": None,
    }
    assert events_count_after == events_count_before


@pytest.mark.parametrize(
    ("home_score", "away_score", "error_message"),
    [
        (-1, 0, "Результат первой команды не может быть отрицательным"),
        (0, -1, "Результат второй команды не может быть отрицательным"),
        (True, 0, "Результат первой команды должен быть целым числом"),
    ],
)
def test_save_match_result_validates_scores_before_writes(
    tmp_path: Path,
    home_score: int,
    away_score: int,
    error_message: str,
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

    with pytest.raises(ValueError, match=error_message):
        save_test_result(
            database_path=database_path,
            contest_id=contest.id,
            match_id=match.id,
            home_score=home_score,
            away_score=away_score,
        )

    with create_connection(database_path) as connection:
        saved_match = connection.execute(
            """
            SELECT status, home_score_final, away_score_final
            FROM matches
            WHERE id = ?
            """,
            (match.id,),
        ).fetchone()
        events_count_after = connection.execute(
            "SELECT COUNT(*) FROM event_log"
        ).fetchone()[0]

    assert dict(saved_match) == {
        "status": "scheduled",
        "home_score_final": None,
        "away_score_final": None,
    }
    assert events_count_after == events_count_before


def test_get_contest_details_returns_leaderboard_with_unique_places(
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
        predicted_advancing_team_id=1,
    )

    for (
        telegram_user_id,
        first_name,
        predicted_home_score,
        predicted_away_score,
        predicted_advancing_team_id,
    ) in (
        (456, "Alice", 3, 2, 1),
        (789, "Bob", 4, 3, 1),
        (101112, "Carol", 0, 1, 2),
    ):
        save_match_prediction(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            contest_id=contest.id,
            match_id=match.id,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=None,
            username=first_name.lower(),
            predicted_home_score=predicted_home_score,
            predicted_away_score=predicted_away_score,
            predicted_advancing_team_id=predicted_advancing_team_id,
            now_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        )

    save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        home_score=2,
        away_score=1,
        advancing_team_id=1,
    )

    contest_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
    )

    leaderboard = [
        (
            entry.place,
            entry.participant_name,
            entry.total_points,
        )
        for entry in contest_details.leaderboard
    ]

    assert [entry[0] for entry in leaderboard] == [1, 2, 3, 4]
    assert leaderboard[0] == (1, "Eugene Sabir", 4)
    assert {entry[1:] for entry in leaderboard[1:3]} == {
        ("Alice", 3),
        ("Bob", 3),
    }
    assert leaderboard[3] == (4, "Carol", 0)


def _leaderboard_sort_test_row(
    *,
    telegram_user_id: int,
    first_name: str,
    total_points: int = 0,
    exact_score_count: int = 0,
    goal_difference_count: int = 0,
    outcome_count: int = 0,
    drawn_advancing_team_count: int = 0,
    correct_champion_count: int = 0,
    match_predictions_count: int = 0,
) -> dict[str, object]:
    return {
        "user_id": telegram_user_id,
        "telegram_user_id": telegram_user_id,
        "first_name": first_name,
        "last_name": None,
        "total_points": total_points,
        "exact_score_count": exact_score_count,
        "goal_difference_count": goal_difference_count,
        "outcome_count": outcome_count,
        "drawn_advancing_team_count": drawn_advancing_team_count,
        "correct_champion_count": correct_champion_count,
        "match_predictions_count": match_predictions_count,
        "champion_prediction_count": 0,
        "total_matches_count": 100,
    }


@pytest.mark.parametrize(
    "criterion",
    (
        "total_points",
        "exact_score_count",
        "goal_difference_count",
        "outcome_count",
        "drawn_advancing_team_count",
        "correct_champion_count",
    ),
)
def test_leaderboard_uses_sporting_tiebreakers_in_strict_order(
    criterion: str,
) -> None:
    criteria = (
        "total_points",
        "exact_score_count",
        "goal_difference_count",
        "outcome_count",
        "drawn_advancing_team_count",
        "correct_champion_count",
    )
    criterion_index = criteria.index(criterion)
    higher_values = {criterion: 2}
    lower_values = {criterion: 1}
    lower_values.update(
        {later_criterion: 99 for later_criterion in criteria[criterion_index + 1 :]}
    )
    rows = (
        _leaderboard_sort_test_row(
            telegram_user_id=200,
            first_name="Alpha",
            **lower_values,
        ),
        _leaderboard_sort_test_row(
            telegram_user_id=100,
            first_name="Zulu",
            **higher_values,
        ),
    )

    leaderboard = contest_service._contest_leaderboard_from_rows(
        rows,
        contest_slug="fixed-contest",
    )

    assert [entry.participant_name for entry in leaderboard] == ["Zulu", "Alpha"]
    assert [entry.place for entry in leaderboard] == [1, 2]
    expected_reason = {
        "total_points": None,
        "exact_score_count": "exact_score",
        "goal_difference_count": "goal_difference",
        "outcome_count": "outcome",
        "drawn_advancing_team_count": "drawn_advancing_team",
        "correct_champion_count": "champion",
    }[criterion]
    assert (
        contest_service.resolve_leaderboard_tiebreak_reason(
            leaderboard[0], leaderboard[1]
        )
        == expected_reason
    )


def test_leaderboard_draw_is_stable_and_ignores_names_and_completeness() -> None:
    contest_slug = "fixed-contest"
    telegram_user_ids = (100, 200)
    first_user_id, second_user_id = sorted(
        telegram_user_ids,
        key=lambda telegram_user_id: contest_service._leaderboard_tiebreak_digest(
            contest_slug=contest_slug,
            telegram_user_id=telegram_user_id,
        ),
    )
    rows = (
        _leaderboard_sort_test_row(
            telegram_user_id=second_user_id,
            first_name="Alpha",
            match_predictions_count=99,
        ),
        _leaderboard_sort_test_row(
            telegram_user_id=first_user_id,
            first_name="Zulu",
            match_predictions_count=0,
        ),
    )

    first_leaderboard = contest_service._contest_leaderboard_from_rows(
        rows,
        contest_slug=contest_slug,
    )
    second_leaderboard = contest_service._contest_leaderboard_from_rows(
        tuple(reversed(rows)),
        contest_slug=contest_slug,
    )

    assert [entry.participant_name for entry in first_leaderboard] == [
        "Zulu",
        "Alpha",
    ]
    assert second_leaderboard == first_leaderboard
    assert [entry.place for entry in first_leaderboard] == [1, 2]
    assert (
        contest_service.resolve_leaderboard_tiebreak_reason(
            first_leaderboard[0], first_leaderboard[1]
        )
        == "draw"
    )
    assert contest_service._leaderboard_tiebreak_digest(
        contest_slug="first-contest",
        telegram_user_id=first_user_id,
    ) != contest_service._leaderboard_tiebreak_digest(
        contest_slug="second-contest",
        telegram_user_id=first_user_id,
    )


def test_leaderboard_draw_uses_telegram_user_id_as_digest_collision_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        contest_service,
        "_leaderboard_tiebreak_digest",
        lambda **_kwargs: b"same-digest",
    )
    rows = (
        _leaderboard_sort_test_row(
            telegram_user_id=200,
            first_name="First row",
        ),
        _leaderboard_sort_test_row(
            telegram_user_id=100,
            first_name="Second row",
        ),
    )

    leaderboard = contest_service._contest_leaderboard_from_rows(
        rows,
        contest_slug="fixed-contest",
    )

    assert [entry.participant_name for entry in leaderboard] == [
        "Second row",
        "First row",
    ]
    assert [entry.place for entry in leaderboard] == [1, 2]


def test_leaderboard_advancing_team_tiebreaker_counts_only_draw_predictions(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
    ).match

    for telegram_user_id, first_name, home_score, away_score in (
        (456, "Draw prediction", 1, 1),
        (789, "Non-draw prediction", 3, 0),
    ):
        save_match_prediction(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            contest_id=contest.id,
            match_id=match.id,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=None,
            username=None,
            predicted_home_score=home_score,
            predicted_away_score=away_score,
            predicted_advancing_team_id=match.home_team_id,
            now_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        )

    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE scoring_rule_sets
            SET outcome_points = 0
            WHERE competition_id = (
                SELECT competitions.id
                FROM competitions
                WHERE competitions.contest_id = ?
            )
            """,
            (contest.id,),
        )

    save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        home_score=2,
        away_score=1,
        advancing_team_id=match.home_team_id,
    )

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
    )

    assert [
        (entry.place, entry.participant_name, entry.total_points)
        for entry in details.leaderboard
    ] == [
        (1, "Draw prediction", 1),
        (2, "Non-draw prediction", 1),
    ]


def test_complete_contest_rejects_unfinished_match(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    contest = create_contest(database_path=database_path).contest
    create_test_match(
        database_path=database_path,
        contest_id=contest.id,
    )

    with pytest.raises(
        ContestCompletionUnavailableError,
        match="Сначала внесите финальные результаты всех матчей.",
    ):
        complete_test_contest(
            database_path=database_path,
            contest_id=contest.id,
        )


def test_concurrent_match_creation_and_completion_preserve_active_invariant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest

    operation_context = local()
    creation_checked_active = Event()
    release_creation = Event()
    release_completion = Event()
    completion_checkpoint: Queue[str] = Queue()
    original_database_connection = contest_service.database_connection
    original_get_active_contest_row = contest_service._get_active_contest_row

    @contextmanager
    def coordinated_database_connection(path: Path):
        with original_database_connection(path) as connection:
            begin_was_reported = False

            def trace_statement(statement: str) -> None:
                nonlocal begin_was_reported
                if (
                    getattr(operation_context, "name", None) == "complete"
                    and statement.strip().upper() == "BEGIN IMMEDIATE"
                    and not begin_was_reported
                ):
                    begin_was_reported = True
                    completion_checkpoint.put("begin_immediate")
                    release_completion.wait(timeout=5)

            connection.set_trace_callback(trace_statement)
            yield connection

    def coordinated_get_active_contest_row(*args, **kwargs):
        row = original_get_active_contest_row(*args, **kwargs)
        operation_name = getattr(operation_context, "name", None)
        if operation_name == "create":
            creation_checked_active.set()
            if not release_creation.wait(timeout=5):
                raise RuntimeError("Timed out while coordinating match creation.")
        elif operation_name == "complete":
            completion_checkpoint.put("active_check")
            if not release_completion.wait(timeout=5):
                raise RuntimeError("Timed out while coordinating contest completion.")
        return row

    monkeypatch.setattr(
        contest_service,
        "database_connection",
        coordinated_database_connection,
    )
    monkeypatch.setattr(
        contest_service,
        "_get_active_contest_row",
        coordinated_get_active_contest_row,
    )

    def create_racing_match() -> Exception | None:
        operation_context.name = "create"
        try:
            create_test_match(
                database_path=database_path,
                contest_id=contest.id,
                starts_at_utc="2099-06-11T18:00:00Z",
            )
        except Exception as error:
            return error
        return None

    def complete_racing_contest() -> Exception | None:
        operation_context.name = "complete"
        try:
            complete_test_contest(
                database_path=database_path,
                contest_id=contest.id,
            )
        except Exception as error:
            return error
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        creation_future = executor.submit(create_racing_match)
        assert creation_checked_active.wait(timeout=5)
        completion_future = executor.submit(complete_racing_contest)
        checkpoint = completion_checkpoint.get(timeout=5)

        if checkpoint == "begin_immediate":
            release_creation.set()
            creation_error = creation_future.result(timeout=5)
            release_completion.set()
            completion_error = completion_future.result(timeout=5)
        else:
            # This branch deterministically recreates the old TOCTOU ordering:
            # completion passes the active check while creation is still paused.
            release_completion.set()
            completion_error = completion_future.result(timeout=5)
            release_creation.set()
            creation_error = creation_future.result(timeout=5)

    with create_connection(database_path) as connection:
        is_active = bool(
            connection.execute(
                "SELECT is_active FROM contests WHERE id = ?",
                (contest.id,),
            ).fetchone()["is_active"]
        )
        match_statuses = [
            str(row["status"])
            for row in connection.execute(
                """
                SELECT matches.status
                FROM matches
                JOIN stages ON stages.id = matches.stage_id
                JOIN competitions ON competitions.id = stages.competition_id
                WHERE competitions.contest_id = ?
                ORDER BY matches.id ASC
                """,
                (contest.id,),
            ).fetchall()
        ]

    assert creation_error is None
    assert isinstance(completion_error, ContestCompletionUnavailableError)
    assert is_active is True
    assert match_statuses == ["scheduled"]


def test_complete_contest_requires_actual_champion_when_enabled(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    contest = create_contest(database_path=database_path).contest

    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE contests
            SET champion_prediction_enabled = 1,
                champion_prediction_deadline_at = '2020-01-01T00:00:00Z'
            WHERE id = ?
            """,
            (contest.id,),
        )

    with pytest.raises(
        ContestCompletionUnavailableError,
        match="Сначала укажите фактического чемпиона.",
    ):
        complete_test_contest(
            database_path=database_path,
            contest_id=contest.id,
        )


def test_complete_contest_moves_it_to_completed_and_preserves_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
    ).match
    save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
    )

    complete_test_contest(
        database_path=database_path,
        contest_id=contest.id,
    )

    active_contests = get_active_contests(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
    )
    completed_contests = get_completed_contests(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
    )
    contest_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
    )

    assert active_contests == ()
    assert [completed_contest.id for completed_contest in completed_contests] == [
        contest.id
    ]
    assert contest_details.is_active is False
    assert len(contest_details.matches) == 1
    assert contest_details.matches[0].result is not None
    assert contest_details.matches[0].result.home_score == 2
    assert contest_details.matches[0].result.away_score == 1

    with create_connection(database_path) as connection:
        completion_event = connection.execute(
            """
            SELECT
                contest_id,
                event_type,
                entity_type,
                entity_id,
                payload_json
            FROM event_log
            WHERE event_type = 'contest.completed'
            """
        ).fetchone()

    assert completion_event is not None
    assert dict(completion_event) == {
        "contest_id": contest.id,
        "event_type": "contest.completed",
        "entity_type": "contest",
        "entity_id": contest.id,
        "payload_json": '{"is_active":false}',
    }

    with pytest.raises(
        ContestCompletedError,
        match="Конкурс завершён. Изменения в нём больше недоступны.",
    ):
        create_test_match(
            database_path=database_path,
            contest_id=contest.id,
            idempotency_key="create-match-after-completion",
        )


def test_delete_contest_deletes_active_contest_and_all_dependent_data(
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
    )

    delete_contest(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        audit_actor=AUDIT_ACTOR,
    )

    assert (
        get_active_contests(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
        )
        == ()
    )

    with create_connection(database_path) as connection:
        for table_name in (
            "contests",
            "contest_creation_requests",
            "competitions",
            "scoring_rule_sets",
            "stages",
            "ties",
            "matches",
            "match_creation_requests",
            "match_predictions",
            "tie_predictions",
            "match_prediction_scores",
            "tie_prediction_scores",
            "champion_predictions",
            "event_log",
        ):
            count = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[
                0
            ]

            assert count == 0, table_name

        chats_count = connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        users_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        teams_count = connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0]

    assert chats_count == 1
    assert users_count == 1
    assert teams_count == 2


def test_delete_contest_rejects_completed_contest(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
    ).match
    save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
    )
    complete_test_contest(
        database_path=database_path,
        contest_id=contest.id,
    )

    with pytest.raises(
        ContestCompletedError,
        match="Завершённый конкурс удалить нельзя",
    ):
        delete_contest(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            contest_id=contest.id,
            audit_actor=AUDIT_ACTOR,
        )

    with create_connection(database_path) as connection:
        contest_row = connection.execute(
            """
            SELECT is_active
            FROM contests
            WHERE id = ?
            """,
            (contest.id,),
        ).fetchone()

    assert contest_row is not None
    assert contest_row["is_active"] == 0


def test_delete_contest_rejects_contest_from_other_chat(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    contest = create_contest(database_path=database_path).contest

    with pytest.raises(ContestNotFoundError, match="Конкурс не найден"):
        delete_contest(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID - 1,
            contest_id=contest.id,
            audit_actor=AUDIT_ACTOR,
        )

    assert get_active_contests(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
    ) == (contest,)


def test_delete_match_removes_linked_data_and_writes_event(
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
    )
    save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
    )

    with create_connection(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ties").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM match_creation_requests"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM match_predictions").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM tie_predictions").fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM match_prediction_scores"
            ).fetchone()[0]
            > 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM tie_prediction_scores").fetchone()[
                0
            ]
            > 0
        )

    delete_test_match(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
    )

    with create_connection(database_path) as connection:
        deleted_event = connection.execute(
            """
            SELECT entity_id, payload_json
            FROM event_log
            WHERE event_type = 'match.deleted'
            """
        ).fetchone()

        assert connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ties").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM match_creation_requests"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM match_predictions").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM tie_predictions").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM match_prediction_scores"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM tie_prediction_scores").fetchone()[
                0
            ]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 2

    assert deleted_event is not None
    assert deleted_event["entity_id"] == match.id

    deleted_payload = json.loads(deleted_event["payload_json"])

    assert deleted_payload["home_team"] == {
        "id": match.home_team_id,
        "name": match.home_team_name,
    }
    assert deleted_payload["away_team"] == {
        "id": match.away_team_id,
        "name": match.away_team_name,
    }
    assert deleted_payload["starts_at_utc"] == match.starts_at_utc
    assert deleted_payload["status"] == "finished"
    assert deleted_payload["tie_id"] == match.tie_id


def test_update_match_start_changes_future_match_and_writes_events(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        starts_at_utc="2030-06-11T18:00:00Z",
    ).match

    updated_match = update_test_match_start(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        starts_at_utc="2030-06-12T20:30:45+02:00",
        now_utc=datetime(2030, 6, 10, 12, 0, tzinfo=timezone.utc),
    )

    assert updated_match.starts_at_utc == "2030-06-12T18:30:45Z"
    with create_connection(database_path) as connection:
        event_log_event = connection.execute(
            """
            SELECT payload_json
            FROM event_log
            WHERE event_type = 'match.updated'
            """
        ).fetchone()
        audit_event = connection.execute(
            """
            SELECT before_state, after_state
            FROM audit_events
            WHERE event_type = 'match_updated'
            """
        ).fetchone()

    assert event_log_event is not None
    assert json.loads(event_log_event["payload_json"]) == {
        "before": {"starts_at_utc": "2030-06-11T18:00:00Z"},
        "after": {"starts_at_utc": "2030-06-12T18:30:45Z"},
    }
    assert audit_event is not None
    assert json.loads(audit_event["before_state"])["starts_at_utc"] == (
        "2030-06-11T18:00:00Z"
    )
    assert json.loads(audit_event["after_state"])["starts_at_utc"] == (
        "2030-06-12T18:30:45Z"
    )


def test_update_match_start_with_same_value_does_not_write_events(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        starts_at_utc="2030-06-11T18:00:00Z",
    ).match

    unchanged_match = update_test_match_start(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
        starts_at_utc="2030-06-11T21:00:00+03:00",
        now_utc=datetime(2030, 6, 10, 12, 0, tzinfo=timezone.utc),
    )

    assert unchanged_match == match
    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM event_log WHERE event_type = 'match.updated'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type = 'match_updated'"
            ).fetchone()[0]
            == 0
        )


def test_update_match_start_resolves_current_time_after_acquiring_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        starts_at_utc="2030-06-11T18:00:00Z",
    ).match
    worker_started = Event()
    time_was_resolved = Event()
    original_resolve_now_utc = contest_service._resolve_now_utc

    def tracked_resolve_now_utc(value: datetime | None) -> datetime:
        time_was_resolved.set()
        return original_resolve_now_utc(value)

    monkeypatch.setattr(
        contest_service,
        "_resolve_now_utc",
        tracked_resolve_now_utc,
    )

    def update_while_locked():
        worker_started.set()
        return update_test_match_start(
            database_path=database_path,
            contest_id=contest.id,
            match_id=match.id,
            starts_at_utc="2030-06-12T18:00:00Z",
            now_utc=datetime(2030, 6, 10, 12, 0, tzinfo=timezone.utc),
        )

    with create_connection(database_path) as lock_connection:
        lock_connection.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            update_future = executor.submit(update_while_locked)
            assert worker_started.wait(timeout=5)
            assert not time_was_resolved.wait(timeout=0.2)
            lock_connection.execute("COMMIT")
            updated_match = update_future.result(timeout=5)

    assert time_was_resolved.is_set()
    assert updated_match.starts_at_utc == "2030-06-12T18:00:00Z"


def test_save_match_prediction_checks_deadline_after_acquiring_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "prediction-deadline-race.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        starts_at_utc="2030-06-11T18:00:00Z",
    ).match
    contender_connected = Event()
    clock = {"now": datetime(2030, 6, 11, 17, 59, 59, tzinfo=timezone.utc)}
    original_database_connection = contest_service.database_connection

    @contextmanager
    def coordinated_database_connection(path: Path):
        with original_database_connection(path) as connection:
            contender_connected.set()
            yield connection

    def controlled_now_utc(_value: datetime | None) -> datetime:
        return clock["now"]

    monkeypatch.setattr(
        contest_service,
        "database_connection",
        coordinated_database_connection,
    )
    monkeypatch.setattr(contest_service, "_resolve_now_utc", controlled_now_utc)

    with create_connection(database_path) as lock_connection:
        lock_connection.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            prediction_future = executor.submit(
                save_match_prediction,
                database_path=database_path,
                telegram_chat_id=TELEGRAM_CHAT_ID,
                contest_id=contest.id,
                match_id=match.id,
                telegram_user_id=TELEGRAM_USER_ID,
                first_name="Eugene",
                last_name="Sabir",
                username="evsab",
                predicted_home_score=2,
                predicted_away_score=1,
                predicted_advancing_team_id=match.home_team_id,
            )
            assert contender_connected.wait(timeout=5)
            clock["now"] = datetime(
                2030,
                6,
                11,
                18,
                0,
                tzinfo=timezone.utc,
            )
            lock_connection.execute("COMMIT")
            with pytest.raises(PredictionUnavailableError, match="уже закрыты"):
                prediction_future.result(timeout=5)

    with create_connection(database_path) as connection:
        prediction_count = connection.execute(
            "SELECT COUNT(*) FROM match_predictions WHERE match_id = ?",
            (match.id,),
        ).fetchone()[0]
    assert prediction_count == 0


def test_publication_enablement_time_is_resolved_after_acquiring_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "publication-enablement-race.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        starts_at_utc="2030-06-11T18:00:00Z",
    )
    contender_connected = Event()
    clock = {"now": datetime(2030, 6, 11, 17, 59, 59, tzinfo=timezone.utc)}
    original_database_connection = contest_service.database_connection

    @contextmanager
    def coordinated_database_connection(path: Path):
        with original_database_connection(path) as connection:
            contender_connected.set()
            yield connection

    def controlled_now_utc(_value: datetime | None) -> datetime:
        return clock["now"]

    monkeypatch.setattr(
        contest_service,
        "database_connection",
        coordinated_database_connection,
    )
    monkeypatch.setattr(contest_service, "_resolve_now_utc", controlled_now_utc)

    with create_connection(database_path) as lock_connection:
        lock_connection.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            enable_future = executor.submit(
                save_match_prediction_publication_settings,
                database_path=database_path,
                telegram_chat_id=TELEGRAM_CHAT_ID,
                contest_id=contest.id,
                telegram_user_id=TELEGRAM_USER_ID,
                first_name="Eugene",
                last_name="Sabir",
                username="evsab",
                enabled=True,
                audit_actor=AUDIT_ACTOR,
            )
            assert contender_connected.wait(timeout=5)
            clock["now"] = datetime(
                2030,
                6,
                11,
                18,
                0,
                tzinfo=timezone.utc,
            )
            lock_connection.execute("COMMIT")
            enable_future.result(timeout=5)

    with create_connection(database_path) as connection:
        enabled_at = connection.execute(
            """
            SELECT match_prediction_publication_enabled_at
            FROM contests
            WHERE id = ?
            """,
            (contest.id,),
        ).fetchone()["match_prediction_publication_enabled_at"]
    assert enabled_at == "2030-06-11T18:00:00Z"


@pytest.mark.parametrize(
    ("now_utc", "new_starts_at_utc", "message"),
    [
        (
            datetime(2030, 6, 11, 18, 0, tzinfo=timezone.utc),
            "2030-06-12T18:00:00Z",
            "уже начавшегося матча",
        ),
        (
            datetime(2030, 6, 10, 12, 0, tzinfo=timezone.utc),
            "2030-06-10T11:59:00Z",
            "должно быть в будущем",
        ),
    ],
)
def test_update_match_start_rejects_closed_match_or_past_new_time(
    tmp_path: Path,
    now_utc: datetime,
    new_starts_at_utc: str,
    message: str,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        starts_at_utc="2030-06-11T18:00:00Z",
    ).match

    with pytest.raises(MatchUpdateUnavailableError, match=message):
        update_test_match_start(
            database_path=database_path,
            contest_id=contest.id,
            match_id=match.id,
            starts_at_utc=new_starts_at_utc,
            now_utc=now_utc,
        )

    with create_connection(database_path) as connection:
        stored_start = connection.execute(
            "SELECT starts_at_utc FROM matches WHERE id = ?",
            (match.id,),
        ).fetchone()["starts_at_utc"]
    assert stored_start == "2030-06-11T18:00:00Z"


def test_delete_match_rejects_unknown_match(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest

    with pytest.raises(MatchNotFoundError, match="Матч не найден"):
        delete_test_match(
            database_path=database_path,
            contest_id=contest.id,
            match_id=999,
        )

    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM event_log WHERE event_type = 'match.deleted'"
            ).fetchone()[0]
            == 0
        )


def test_delete_match_rejects_completed_contest(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest
    match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
    ).match

    save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=match.id,
    )
    complete_test_contest(
        database_path=database_path,
        contest_id=contest.id,
    )

    with pytest.raises(
        ContestCompletedError,
        match="Конкурс завершён. Изменения в нём больше недоступны.",
    ):
        delete_test_match(
            database_path=database_path,
            contest_id=contest.id,
            match_id=match.id,
        )

    with create_connection(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ties").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM event_log WHERE event_type = 'match.deleted'"
            ).fetchone()[0]
            == 0
        )


def test_contest_details_leaderboard_history_shows_only_closed_predictions(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest

    past_match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        starts_at_utc="2026-06-10T18:00:00Z",
        idempotency_key="create-past-match",
    ).match
    current_match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        starts_at_utc="2026-06-11T18:00:00Z",
        idempotency_key="create-current-match",
    ).match
    future_match = create_test_match(
        database_path=database_path,
        contest_id=contest.id,
        starts_at_utc="2099-06-12T18:00:00Z",
        idempotency_key="create-future-match",
    ).match

    save_test_prediction(
        database_path=database_path,
        contest_id=contest.id,
        match_id=past_match.id,
        predicted_home_score=2,
        predicted_away_score=1,
        predicted_advancing_team_id=1,
        now_utc=datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
    )
    save_test_prediction(
        database_path=database_path,
        contest_id=contest.id,
        match_id=current_match.id,
        predicted_home_score=1,
        predicted_away_score=1,
        predicted_advancing_team_id=2,
        now_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
    )
    save_test_prediction(
        database_path=database_path,
        contest_id=contest.id,
        match_id=future_match.id,
        predicted_home_score=0,
        predicted_away_score=2,
        predicted_advancing_team_id=2,
        now_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
    )
    save_test_result(
        database_path=database_path,
        contest_id=contest.id,
        match_id=past_match.id,
        home_score=2,
        away_score=1,
        advancing_team_id=1,
        now_utc=datetime(2026, 6, 10, 18, 0, tzinfo=timezone.utc),
    )

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
    )

    participant = next(
        entry
        for entry in details.leaderboard
        if entry.participant_name == "Eugene Sabir"
    )

    assert [match.id for match in participant.prediction_history] == [
        current_match.id,
        past_match.id,
    ]
    assert participant.prediction_history[0].result is None
    assert participant.prediction_history[0].prediction_score is None
    assert participant.prediction_history[1].result is not None
    assert participant.prediction_history[1].prediction_score is not None
    assert participant.prediction_history[1].prediction_score.total_points == 4


def test_match_prediction_publication_settings_are_disabled_by_default_and_update(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_contest(database_path=database_path).contest

    initial_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
    )

    assert initial_details.match_prediction_publication.is_enabled is False

    enabled_at = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    save_match_prediction_publication_settings(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        enabled=True,
        now_utc=enabled_at,
        audit_actor=AUDIT_ACTOR,
    )

    enabled_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
    )
    assert enabled_details.match_prediction_publication.is_enabled is True

    with create_connection(database_path) as connection:
        enabled_row = connection.execute(
            """
            SELECT
                match_prediction_publication_enabled,
                match_prediction_publication_enabled_at
            FROM contests
            WHERE id = ?
            """,
            (contest.id,),
        ).fetchone()
        enabled_event = connection.execute(
            """
            SELECT payload_json
            FROM event_log
            WHERE event_type =
                'contest.match_prediction_publication_settings_updated'
            """
        ).fetchone()

    assert dict(enabled_row) == {
        "match_prediction_publication_enabled": 1,
        "match_prediction_publication_enabled_at": "2026-06-10T12:00:00Z",
    }
    assert json.loads(enabled_event["payload_json"]) == {
        "enabled": True,
        "previous_enabled": False,
    }

    save_match_prediction_publication_settings(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        enabled=False,
        now_utc=enabled_at,
        audit_actor=AUDIT_ACTOR,
    )

    disabled_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
    )
    assert disabled_details.match_prediction_publication.is_enabled is False

    with create_connection(database_path) as connection:
        disabled_row = connection.execute(
            """
            SELECT
                match_prediction_publication_enabled,
                match_prediction_publication_enabled_at
            FROM contests
            WHERE id = ?
            """,
            (contest.id,),
        ).fetchone()

    assert dict(disabled_row) == {
        "match_prediction_publication_enabled": 0,
        "match_prediction_publication_enabled_at": None,
    }
