from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier

import pytest

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    ChampionPredictionSettingsLockedError,
    ChampionUnavailableError,
    PredictionUnavailableError,
    complete_contest,
    create_match,
    create_world_cup_2026_contest,
    get_contest_details,
    save_champion_prediction,
    save_champion_prediction_settings,
    save_contest_champion,
)
from app.database import database_connection, initialize_database
from tests.support import ensure_contest_teams


CHAT_ID = -1001234567890
ADMIN_TELEGRAM_USER_ID = 101
ALICE_TELEGRAM_USER_ID = 202
BOB_TELEGRAM_USER_ID = 303
OPEN_PREDICTION_TIME = datetime(2029, 1, 1, tzinfo=timezone.utc)
CLOSED_PREDICTION_TIME = datetime(2030, 1, 2, tzinfo=timezone.utc)
FUTURE_DEADLINE = "2030-01-01T12:00:00Z"
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=CHAT_ID,
    telegram_user_id=ADMIN_TELEGRAM_USER_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "predictor.db"
    initialize_database(path)
    return path


def _create_contest(database_path: Path) -> int:
    result = create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=ADMIN_TELEGRAM_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        contest_name="Плей-офф",
        idempotency_key="create-contest",
        audit_actor=AUDIT_ACTOR,
    )
    return result.contest.id


def _create_matches(
    database_path: Path,
    *,
    contest_id: int,
) -> tuple[int, int, int, int]:
    (
        spain_team_id,
        france_team_id,
        germany_team_id,
        portugal_team_id,
    ) = ensure_contest_teams(
        database_path,
        contest_id=contest_id,
        names=("Испания", "Франция", "Германия", "Португалия"),
    )
    first_match = create_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_TELEGRAM_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        home_team_id=spain_team_id,
        away_team_id=france_team_id,
        starts_at_utc="2029-06-14T12:00:00Z",
        idempotency_key="create-first-match",
        audit_actor=AUDIT_ACTOR,
    ).match
    second_match = create_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_TELEGRAM_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        home_team_id=germany_team_id,
        away_team_id=portugal_team_id,
        starts_at_utc="2029-06-15T12:00:00Z",
        idempotency_key="create-second-match",
        audit_actor=AUDIT_ACTOR,
    ).match

    return (
        first_match.id,
        first_match.home_team_id,
        first_match.away_team_id,
        second_match.id,
    )


def _configure_champion_prediction(
    database_path: Path,
    *,
    contest_id: int,
    deadline_at: str = FUTURE_DEADLINE,
    points: int = 5,
) -> None:
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_TELEGRAM_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        enabled=True,
        deadline_at=deadline_at,
        points=points,
        audit_actor=AUDIT_ACTOR,
    )


def _mark_all_matches_finished(
    database_path: Path,
    *,
    contest_id: int,
) -> None:
    with database_connection(database_path) as connection:
        match_rows = connection.execute(
            """
            SELECT
                matches.id,
                matches.tie_id,
                matches.home_team_id,
                matches.away_team_id
            FROM matches
            JOIN stages
                ON stages.id = matches.stage_id
            JOIN competitions
                ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ?
            ORDER BY matches.id ASC
            """,
            (contest_id,),
        ).fetchall()

        for match_row in match_rows:
            connection.execute(
                """
                UPDATE matches
                SET
                    status = 'finished',
                    home_score_final = 1,
                    away_score_final = 0
                WHERE id = ?
                """,
                (match_row["id"],),
            )
            connection.execute(
                """
                UPDATE ties
                SET advancing_team_id = ?
                WHERE id = ?
                """,
                (match_row["home_team_id"], match_row["tie_id"]),
            )


def test_champion_prediction_card_lists_candidates_and_saves_selection(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    _, spain_team_id, _, _ = _create_matches(
        database_path,
        contest_id=contest_id,
    )
    _configure_champion_prediction(
        database_path,
        contest_id=contest_id,
        points=7,
    )

    details_before_prediction = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_TELEGRAM_USER_ID,
    )

    assert details_before_prediction.champion_prediction.is_enabled is True
    assert details_before_prediction.champion_prediction.deadline_at == FUTURE_DEADLINE
    assert details_before_prediction.champion_prediction.points == 7
    assert details_before_prediction.champion_prediction.is_open is True
    assert details_before_prediction.champion_prediction.prediction is None
    assert details_before_prediction.champion_prediction.actual_champion is None
    assert {
        candidate.name
        for candidate in details_before_prediction.champion_prediction.candidates
    } == {"Германия", "Испания", "Португалия", "Франция"}

    selected_team = save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_TELEGRAM_USER_ID,
        first_name="Алиса",
        last_name="Иванова",
        username="alice",
        predicted_team_id=spain_team_id,
        now_utc=OPEN_PREDICTION_TIME,
    )

    assert selected_team.id == spain_team_id
    assert selected_team.name == "Испания"

    details_after_prediction = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_TELEGRAM_USER_ID,
    )
    assert details_after_prediction.champion_prediction.prediction == selected_team
    assert details_after_prediction.champion_prediction.awarded_points is None

    with database_connection(database_path) as connection:
        prediction_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM champion_predictions
            """
        ).fetchone()[0]

    assert prediction_count == 1


def test_champion_can_be_recorded_without_matches_after_deadline(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    spain_team_id, _ = ensure_contest_teams(
        database_path,
        contest_id=contest_id,
        names=("Spain", "France"),
    )
    _configure_champion_prediction(
        database_path,
        contest_id=contest_id,
    )
    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_TELEGRAM_USER_ID,
        first_name="Alice",
        last_name=None,
        username="alice",
        predicted_team_id=spain_team_id,
        now_utc=OPEN_PREDICTION_TIME,
    )

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_TELEGRAM_USER_ID,
        now_utc=CLOSED_PREDICTION_TIME,
    )
    assert details.matches == ()
    assert details.champion_prediction.is_tournament_completed is True

    champion = save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_TELEGRAM_USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        champion_team_id=spain_team_id,
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_PREDICTION_TIME,
    )
    assert champion.id == spain_team_id

    updated_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_TELEGRAM_USER_ID,
        now_utc=CLOSED_PREDICTION_TIME,
    )
    assert updated_details.champion_prediction.actual_champion == champion
    assert updated_details.champion_prediction.awarded_points == 5


def test_champion_prediction_rejects_non_candidate_and_closed_deadline(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    _, spain_team_id, _, _ = _create_matches(
        database_path,
        contest_id=contest_id,
    )
    _configure_champion_prediction(
        database_path,
        contest_id=contest_id,
    )

    with database_connection(database_path) as connection:
        unrelated_team_id = int(
            connection.execute(
                """
                INSERT INTO teams (name)
                VALUES (?)
                """,
                ("Аргентина",),
            ).lastrowid
        )

    with pytest.raises(
        ValueError,
        match="Выбранная команда не участвует в этом конкурсе.",
    ):
        save_champion_prediction(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=ALICE_TELEGRAM_USER_ID,
            first_name="Алиса",
            last_name=None,
            username="alice",
            predicted_team_id=unrelated_team_id,
            now_utc=OPEN_PREDICTION_TIME,
        )

    with pytest.raises(
        PredictionUnavailableError,
        match="Прогноз на чемпиона уже закрыт.",
    ):
        save_champion_prediction(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=ALICE_TELEGRAM_USER_ID,
            first_name="Алиса",
            last_name=None,
            username="alice",
            predicted_team_id=spain_team_id,
            now_utc=CLOSED_PREDICTION_TIME,
        )


def test_champion_recalculation_updates_leaderboard_after_correction(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    _, spain_team_id, france_team_id, _ = _create_matches(
        database_path,
        contest_id=contest_id,
    )
    _configure_champion_prediction(
        database_path,
        contest_id=contest_id,
    )

    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_TELEGRAM_USER_ID,
        first_name="Алиса",
        last_name=None,
        username="alice",
        predicted_team_id=spain_team_id,
        now_utc=OPEN_PREDICTION_TIME,
    )
    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=BOB_TELEGRAM_USER_ID,
        first_name="Боб",
        last_name=None,
        username="bob",
        predicted_team_id=france_team_id,
        now_utc=OPEN_PREDICTION_TIME,
    )

    with pytest.raises(
        ChampionUnavailableError,
        match="Чемпиона можно указать после завершения всех матчей конкурса.",
    ):
        save_contest_champion(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=ADMIN_TELEGRAM_USER_ID,
            first_name="Администратор",
            last_name=None,
            username="admin",
            champion_team_id=spain_team_id,
            audit_actor=AUDIT_ACTOR,
        )

    _mark_all_matches_finished(
        database_path,
        contest_id=contest_id,
    )

    with pytest.raises(
        ChampionUnavailableError,
        match=(
            "Фактического чемпиона можно указать после закрытия прогнозов на чемпиона."
        ),
    ):
        save_contest_champion(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=ADMIN_TELEGRAM_USER_ID,
            first_name="Администратор",
            last_name=None,
            username="admin",
            champion_team_id=spain_team_id,
            now_utc=OPEN_PREDICTION_TIME,
            audit_actor=AUDIT_ACTOR,
        )

    with database_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT champion_team_id FROM contests WHERE id = ?",
                (contest_id,),
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM event_log
            WHERE contest_id = ?
              AND event_type IN (
                  'contest.champion_recorded',
                  'contest.champion_corrected'
              )
            """,
                (contest_id,),
            ).fetchone()[0]
            == 0
        )

    saved_champion = save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_TELEGRAM_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        champion_team_id=spain_team_id,
        now_utc=CLOSED_PREDICTION_TIME,
        audit_actor=AUDIT_ACTOR,
    )
    assert saved_champion.name == "Испания"

    alice_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_TELEGRAM_USER_ID,
    )
    assert alice_details.champion_prediction.actual_champion == saved_champion
    assert alice_details.champion_prediction.awarded_points == 5
    assert [
        (entry.participant_name, entry.total_points)
        for entry in alice_details.leaderboard
    ] == [("Алиса", 5), ("Боб", 0)]

    corrected_champion = save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_TELEGRAM_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        champion_team_id=france_team_id,
        now_utc=CLOSED_PREDICTION_TIME,
        audit_actor=AUDIT_ACTOR,
    )
    assert corrected_champion.name == "Франция"

    corrected_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_TELEGRAM_USER_ID,
    )
    assert corrected_details.champion_prediction.awarded_points == 0
    assert [
        (entry.participant_name, entry.total_points)
        for entry in corrected_details.leaderboard
    ] == [("Боб", 5), ("Алиса", 0)]


def test_champion_deadline_cannot_be_moved_after_it_has_passed(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    _, spain_team_id, _, _ = _create_matches(
        database_path,
        contest_id=contest_id,
    )
    _configure_champion_prediction(database_path, contest_id=contest_id)
    _mark_all_matches_finished(database_path, contest_id=contest_id)

    with pytest.raises(
        ChampionPredictionSettingsLockedError,
        match="нельзя изменить после его наступления",
    ):
        save_champion_prediction_settings(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=ADMIN_TELEGRAM_USER_ID,
            first_name="Администратор",
            last_name=None,
            username="admin",
            enabled=True,
            deadline_at="2031-01-01T12:00:00Z",
            points=5,
            now_utc=CLOSED_PREDICTION_TIME,
            audit_actor=AUDIT_ACTOR,
        )

    champion = save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_TELEGRAM_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        champion_team_id=spain_team_id,
        now_utc=CLOSED_PREDICTION_TIME,
        audit_actor=AUDIT_ACTOR,
    )
    assert champion.id == spain_team_id


def test_champion_deadline_can_be_changed_before_it_passes(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    _create_matches(database_path, contest_id=contest_id)
    _configure_champion_prediction(database_path, contest_id=contest_id)

    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_TELEGRAM_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        enabled=True,
        deadline_at="2030-02-01T12:00:00Z",
        points=5,
        now_utc=OPEN_PREDICTION_TIME,
        audit_actor=AUDIT_ACTOR,
    )

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=OPEN_PREDICTION_TIME,
    )
    assert details.champion_prediction.deadline_at == "2030-02-01T12:00:00Z"


def test_concurrent_champion_and_settings_changes_preserve_invariant(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    _, spain_team_id, _, _ = _create_matches(
        database_path,
        contest_id=contest_id,
    )
    _configure_champion_prediction(database_path, contest_id=contest_id)
    _mark_all_matches_finished(database_path, contest_id=contest_id)
    barrier = Barrier(2)

    def move_deadline() -> tuple[str, str, str]:
        barrier.wait()
        try:
            save_champion_prediction_settings(
                database_path=database_path,
                telegram_chat_id=CHAT_ID,
                contest_id=contest_id,
                telegram_user_id=ADMIN_TELEGRAM_USER_ID,
                first_name="Администратор",
                last_name=None,
                username="admin",
                enabled=True,
                deadline_at="2031-01-01T12:00:00Z",
                points=5,
                now_utc=CLOSED_PREDICTION_TIME,
                audit_actor=AUDIT_ACTOR,
            )
        except ValueError as error:
            return "settings", type(error).__name__, str(error)
        return "settings", "ok", ""

    def record_champion() -> tuple[str, str, str]:
        barrier.wait()
        try:
            save_contest_champion(
                database_path=database_path,
                telegram_chat_id=CHAT_ID,
                contest_id=contest_id,
                telegram_user_id=ADMIN_TELEGRAM_USER_ID,
                first_name="Администратор",
                last_name=None,
                username="admin",
                champion_team_id=spain_team_id,
                now_utc=CLOSED_PREDICTION_TIME,
                audit_actor=AUDIT_ACTOR,
            )
        except ValueError as error:
            return "champion", type(error).__name__, str(error)
        return "champion", "ok", ""

    with ThreadPoolExecutor(max_workers=2) as executor:
        settings_future = executor.submit(move_deadline)
        champion_future = executor.submit(record_champion)
        results = {
            result[0]: result[1:]
            for result in (
                settings_future.result(),
                champion_future.result(),
            )
        }

    assert sum(status == "ok" for status, _ in results.values()) == 1
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT champion_prediction_deadline_at, champion_team_id
            FROM contests
            WHERE id = ?
            """,
            (contest_id,),
        ).fetchone()

    if row["champion_team_id"] is None:
        assert row["champion_prediction_deadline_at"] == "2031-01-01T12:00:00Z"
        assert results["settings"] == ("ok", "")
        assert results["champion"][0] == "ChampionUnavailableError"
    else:
        assert row["champion_team_id"] == spain_team_id
        assert row["champion_prediction_deadline_at"] == FUTURE_DEADLINE
        assert results["champion"] == ("ok", "")
        assert results["settings"][0] == "ChampionPredictionSettingsLockedError"


def test_complete_contest_rejects_enabled_champion_prediction_without_deadline(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    _, spain_team_id, _, _ = _create_matches(
        database_path,
        contest_id=contest_id,
    )
    _configure_champion_prediction(database_path, contest_id=contest_id)
    _mark_all_matches_finished(database_path, contest_id=contest_id)
    with database_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE contests
            SET champion_prediction_deadline_at = NULL,
                champion_team_id = ?
            WHERE id = ?
            """,
            (spain_team_id, contest_id),
        )

    with pytest.raises(
        ValueError,
        match="Сначала укажите дедлайн прогноза на чемпиона",
    ):
        complete_contest(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=ADMIN_TELEGRAM_USER_ID,
            first_name="Администратор",
            last_name=None,
            username="admin",
            now_utc=CLOSED_PREDICTION_TIME,
            audit_actor=AUDIT_ACTOR,
        )

    with database_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT is_active FROM contests WHERE id = ?",
                (contest_id,),
            ).fetchone()[0]
            == 1
        )


def test_correct_champion_breaks_tie_with_zero_points_in_completed_contest(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    _, spain_team_id, france_team_id, _ = _create_matches(
        database_path,
        contest_id=contest_id,
    )
    _configure_champion_prediction(
        database_path,
        contest_id=contest_id,
        points=0,
    )

    for telegram_user_id, first_name, username, predicted_team_id in (
        (ALICE_TELEGRAM_USER_ID, "Алиса", "alice", spain_team_id),
        (BOB_TELEGRAM_USER_ID, "Боб", "bob", france_team_id),
    ):
        save_champion_prediction(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=None,
            username=username,
            predicted_team_id=predicted_team_id,
            now_utc=OPEN_PREDICTION_TIME,
        )

    _mark_all_matches_finished(database_path, contest_id=contest_id)
    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_TELEGRAM_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        champion_team_id=spain_team_id,
        now_utc=CLOSED_PREDICTION_TIME,
        audit_actor=AUDIT_ACTOR,
    )

    active_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
    )
    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_TELEGRAM_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        now_utc=CLOSED_PREDICTION_TIME,
        audit_actor=AUDIT_ACTOR,
    )
    completed_details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
    )

    expected_leaderboard = [(1, "Алиса", 0), (2, "Боб", 0)]
    assert [
        (entry.place, entry.participant_name, entry.total_points)
        for entry in active_details.leaderboard
    ] == expected_leaderboard
    assert [
        (entry.place, entry.participant_name, entry.total_points)
        for entry in completed_details.leaderboard
    ] == expected_leaderboard
