from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import pytest

from app import shared_tournament_service
from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    complete_contest,
    SharedTournamentManagedError,
    create_champions_league_2026_27_contest,
    create_world_cup_2026_contest,
    save_champion_prediction,
    save_match_prediction,
    save_match_prediction_publication_settings,
    save_swiss_stage_prediction,
    update_match_start,
)
from app.database import create_connection, initialize_database
from app.shared_tournament_service import (
    SharedMatchConflictError,
    SharedMatchUpdateUnavailableError,
    SharedTournamentCompletionUnavailableError,
    SharedTournamentConflictError,
    SharedTournamentLockedError,
    SharedTournamentResultUnavailableError,
    archive_shared_tournament,
    create_shared_match,
    create_shared_tournament,
    delete_shared_match,
    get_shared_tournament_details,
    restore_shared_tournament,
    save_shared_champion_result,
    save_shared_champion_settings,
    save_shared_match_result,
    save_shared_swiss_result,
    save_shared_swiss_settings,
    save_shared_tournament_teams,
    update_shared_match_start,
)


OWNER_ID = 123


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _create_shared_tournament(database_path: Path):
    details = create_shared_tournament(
        database_path=database_path,
        name="Чемпионат мира 2026",
        template_key="world_cup_2026",
        actor_telegram_user_id=OWNER_ID,
    )
    details = save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        team_names=["Испания", "Франция"],
        expected_version=details.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )
    return details


def _create_contest(
    database_path: Path,
    *,
    shared_tournament_id: int,
    chat_id: int,
    suffix: str,
):
    return create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=chat_id,
        chat_title=f"Чат {suffix}",
        telegram_user_id=OWNER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name=f"Прогнозы {suffix}",
        idempotency_key=f"contest-{suffix}",
        audit_actor=AuditActor(
            telegram_chat_id=chat_id,
            telegram_user_id=OWNER_ID,
            role=AuditActorRole.TELEGRAM_ADMIN,
        ),
        shared_tournament_id=shared_tournament_id,
    ).contest


def _local_match_ids(database_path: Path, shared_match_id: int) -> dict[int, int]:
    with create_connection(database_path) as connection:
        return {
            int(row["contest_id"]): int(row["match_id"])
            for row in connection.execute(
                """
                SELECT contest_id, match_id
                FROM shared_match_links
                WHERE shared_match_id = ?
                ORDER BY contest_id
                """,
                (shared_match_id,),
            )
        }


def test_champions_league_shared_defaults_to_eight_direct_plus_twelve_eliminated(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "champions-league.db"
    initialize_database(database_path)
    shared = create_shared_tournament(
        database_path=database_path,
        name="Лига чемпионов 2026/27",
        template_key="champions_league_2026_27",
        actor_telegram_user_id=OWNER_ID,
    )

    assert shared.matches == ()
    assert shared.swiss_stage_prediction.is_enabled is False
    assert shared.swiss_stage_prediction.direct_qualifier_count == 8
    assert shared.swiss_stage_prediction.elimination_qualifier_count == 12
    assert shared.swiss_stage_prediction.selection_mode == "up_to_limits"
    assert shared.swiss_stage_prediction.direct_correct_points == 2
    assert shared.swiss_stage_prediction.elimination_correct_points == 1
    assert shared.swiss_stage_prediction.cross_category_points == 0
    assert shared.swiss_stage_prediction.maximum_points == 28

    first_35_teams = [f"Команда {number:02d}" for number in range(1, 36)]
    shared = save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        team_names=first_35_teams,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )
    with pytest.raises(
        SharedTournamentResultUnavailableError,
        match="общий этап",
    ):
        save_shared_swiss_result(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            direct_team_ids=[],
            elimination_team_ids=[],
            expected_version=shared.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            actor_first_name="Eugene",
            actor_last_name="Sabir",
            actor_username="evsab",
            now_utc=_time("2030-08-01T00:00:00Z"),
        )
    with pytest.raises(ValueError, match="ровно 36 команд"):
        save_shared_swiss_settings(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            enabled=True,
            deadline_at="2030-09-01T12:00:00Z",
            direct_qualifier_count=8,
            elimination_qualifier_count=12,
            expected_version=shared.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            actor_first_name="Eugene",
            actor_last_name="Sabir",
            actor_username="evsab",
            now_utc=_time("2030-08-01T00:00:00Z"),
        )

    shared = save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        team_names=[*first_35_teams, "Команда 36"],
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )
    with pytest.raises(ValueError, match="8 команд напрямую"):
        save_shared_swiss_settings(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            enabled=True,
            deadline_at="2030-09-01T12:00:00Z",
            direct_qualifier_count=7,
            elimination_qualifier_count=12,
            expected_version=shared.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            actor_first_name="Eugene",
            actor_last_name="Sabir",
            actor_username="evsab",
            now_utc=_time("2030-08-01T00:00:00Z"),
        )

    shared = save_shared_swiss_settings(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        enabled=True,
        deadline_at="2030-09-01T12:00:00Z",
        direct_qualifier_count=8,
        elimination_qualifier_count=12,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-08-01T00:00:00Z"),
    )
    with pytest.raises(ValueError, match="ровно 36 команд"):
        save_shared_tournament_teams(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            team_names=first_35_teams,
            expected_version=shared.tournament.version,
            actor_telegram_user_id=OWNER_ID,
        )
    shared = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    assert len(shared.teams) == 36
    contest = create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=-1001,
        chat_title="Чат ЛЧ",
        telegram_user_id=OWNER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name="Прогнозы ЛЧ",
        idempotency_key="ucl-shared-contest",
        audit_actor=AuditActor(
            telegram_chat_id=-1001,
            telegram_user_id=OWNER_ID,
            role=AuditActorRole.TELEGRAM_ADMIN,
        ),
        shared_tournament_id=shared.tournament.id,
    ).contest
    second_contest = create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=-1002,
        chat_title="Второй чат ЛЧ",
        telegram_user_id=OWNER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name="Прогнозы ЛЧ — второй чат",
        idempotency_key="ucl-shared-contest-2",
        audit_actor=AuditActor(
            telegram_chat_id=-1002,
            telegram_user_id=OWNER_ID,
            role=AuditActorRole.TELEGRAM_ADMIN,
        ),
        shared_tournament_id=shared.tournament.id,
    ).contest

    with create_connection(database_path) as connection:
        settings = connection.execute(
            """
            SELECT
                contest_id,
                enabled,
                direct_qualifier_count,
                elimination_qualifier_count,
                selection_mode,
                direct_correct_points,
                elimination_correct_points,
                cross_category_points
            FROM swiss_stage_prediction_settings
            WHERE contest_id IN (?, ?)
            ORDER BY contest_id
            """,
            (contest.id, second_contest.id),
        ).fetchall()
        match_count = connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    assert [tuple(row) for row in settings] == [
        (contest.id, 1, 8, 12, "up_to_limits", 2, 1, 0),
        (second_contest.id, 1, 8, 12, "up_to_limits", 2, 1, 0),
    ]
    assert match_count == 0

    shared = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE swiss_stage_prediction_settings
            SET selection_mode = 'exact',
                direct_correct_points = 9,
                elimination_correct_points = 9,
                cross_category_points = 9
            WHERE contest_id = ?
            """,
            (contest.id,),
        )
    shared = save_shared_swiss_settings(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        enabled=True,
        deadline_at="2030-09-01T13:00:00Z",
        direct_qualifier_count=8,
        elimination_qualifier_count=12,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-08-01T00:00:00Z"),
    )
    with create_connection(database_path) as connection:
        synchronized_policies = {
            tuple(row)
            for row in connection.execute(
                """
                SELECT selection_mode, direct_correct_points,
                       elimination_correct_points, cross_category_points
                FROM swiss_stage_prediction_settings
                WHERE contest_id IN (?, ?)
                """,
                (contest.id, second_contest.id),
            )
        }
    assert synchronized_policies == {("up_to_limits", 2, 1, 0)}
    team_ids = [team.id for team in shared.teams]
    with pytest.raises(ValueError, match="не соответствует"):
        save_shared_swiss_result(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            direct_team_ids=team_ids[:7],
            elimination_team_ids=team_ids[8:20],
            expected_version=shared.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            actor_first_name="Eugene",
            actor_last_name="Sabir",
            actor_username="evsab",
            now_utc=_time("2030-09-02T00:00:00Z"),
        )
    saved_result = save_shared_swiss_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        direct_team_ids=team_ids[:8],
        elimination_team_ids=team_ids[8:20],
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-09-02T00:00:00Z"),
    )
    assert saved_result.swiss_stage_prediction.playoff_team_ids == tuple(team_ids[20:])


def test_legacy_shared_template_keeps_exact_swiss_scoring_policy(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "world-cup-policy.db"
    initialize_database(database_path)

    shared = create_shared_tournament(
        database_path=database_path,
        name="Чемпионат мира 2026",
        template_key="world_cup_2026",
        actor_telegram_user_id=OWNER_ID,
    )

    assert shared.swiss_stage_prediction.selection_mode == "exact"
    assert shared.swiss_stage_prediction.direct_correct_points == 2
    assert shared.swiss_stage_prediction.elimination_correct_points == 2
    assert shared.swiss_stage_prediction.cross_category_points == 1
    assert shared.swiss_stage_prediction.maximum_points == 16


def test_shared_team_lock_is_exposed_and_preserves_unlinked_stage_result(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "shared-team-lock.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    assert shared.teams_locked is False

    configured = save_shared_swiss_settings(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        enabled=True,
        deadline_at="2030-06-01T12:00:00Z",
        direct_qualifier_count=1,
        elimination_qualifier_count=1,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-05-01T00:00:00Z"),
    )
    resulted = save_shared_swiss_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        direct_team_ids=[shared.teams[0].id],
        elimination_team_ids=[shared.teams[1].id],
        expected_version=configured.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T12:00:00Z"),
    )

    assert resulted.teams_locked is True
    with pytest.raises(SharedTournamentLockedError, match="заблокирован"):
        save_shared_tournament_teams(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            team_names=["Испания", "Германия"],
            expected_version=resulted.tournament.version,
            actor_telegram_user_id=OWNER_ID,
        )


def test_shared_versioned_writes_accept_only_exact_single_step_retries(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "shared-exact-retries.db"
    initialize_database(database_path)
    initial = create_shared_tournament(
        database_path=database_path,
        name="Чемпионат мира 2026",
        template_key="world_cup_2026",
        actor_telegram_user_id=OWNER_ID,
    )

    def shared_event_count() -> int:
        with create_connection(database_path) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM shared_tournament_events"
                ).fetchone()[0]
            )

    teams = save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        team_names=["Испания", "Франция"],
        expected_version=initial.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )
    events_after_teams = shared_event_count()
    repeated_teams = save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        team_names=["Испания", "Франция"],
        expected_version=initial.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )
    assert repeated_teams == teams
    assert shared_event_count() == events_after_teams

    champion_settings = save_shared_champion_settings(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        enabled=True,
        deadline_at="2030-06-01T12:00:00Z",
        points=7,
        expected_version=teams.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-05-01T00:00:00Z"),
    )
    events_after_champion_settings = shared_event_count()
    repeated_champion_settings = save_shared_champion_settings(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        enabled=True,
        deadline_at="2030-06-01T12:00:00Z",
        points=7,
        expected_version=teams.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-05-01T00:01:00Z"),
    )
    assert repeated_champion_settings == champion_settings
    assert shared_event_count() == events_after_champion_settings

    with pytest.raises(SharedTournamentConflictError, match="уже был изменён"):
        save_shared_tournament_teams(
            database_path=database_path,
            shared_tournament_id=initial.tournament.id,
            team_names=["Испания", "Франция"],
            expected_version=teams.tournament.version,
            actor_telegram_user_id=OWNER_ID,
        )

    with pytest.raises(SharedTournamentConflictError, match="уже был изменён"):
        save_shared_tournament_teams(
            database_path=database_path,
            shared_tournament_id=initial.tournament.id,
            team_names=["Испания", "Франция"],
            expected_version=initial.tournament.version,
            actor_telegram_user_id=OWNER_ID,
        )

    swiss_settings = save_shared_swiss_settings(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        enabled=True,
        deadline_at="2030-06-01T12:00:00Z",
        direct_qualifier_count=1,
        elimination_qualifier_count=1,
        expected_version=champion_settings.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-05-01T00:00:00Z"),
    )
    events_after_swiss_settings = shared_event_count()
    repeated_swiss_settings = save_shared_swiss_settings(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        enabled=True,
        deadline_at="2030-06-01T12:00:00Z",
        direct_qualifier_count=1,
        elimination_qualifier_count=1,
        expected_version=champion_settings.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-05-01T00:01:00Z"),
    )
    assert repeated_swiss_settings == swiss_settings
    assert shared_event_count() == events_after_swiss_settings

    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        home_team_id=teams.teams[0].id,
        away_team_id=teams.teams[1].id,
        starts_at_utc="2030-06-01T13:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-05-01T00:00:00Z"),
    )
    saved_match = save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        shared_match_id=match.id,
        home_score=2,
        away_score=1,
        advancing_team_id=teams.teams[0].id,
        expected_version=match.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T14:00:00Z"),
    )
    events_after_match_result = shared_event_count()
    repeated_match = save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        shared_match_id=match.id,
        home_score=2,
        away_score=1,
        advancing_team_id=teams.teams[0].id,
        expected_version=match.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T14:01:00Z"),
    )
    assert repeated_match == saved_match
    assert shared_event_count() == events_after_match_result

    before_champion_result = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
    )
    champion = save_shared_champion_result(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        champion_team_id=teams.teams[0].id,
        expected_version=before_champion_result.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T14:00:00Z"),
    )
    events_after_champion = shared_event_count()
    repeated_champion = save_shared_champion_result(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        champion_team_id=teams.teams[0].id,
        expected_version=before_champion_result.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T14:01:00Z"),
    )
    assert repeated_champion == champion
    assert shared_event_count() == events_after_champion

    swiss = save_shared_swiss_result(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        direct_team_ids=[teams.teams[0].id],
        elimination_team_ids=[teams.teams[1].id],
        expected_version=champion.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T14:00:00Z"),
    )
    events_after_swiss = shared_event_count()
    repeated_swiss = save_shared_swiss_result(
        database_path=database_path,
        shared_tournament_id=initial.tournament.id,
        direct_team_ids=[teams.teams[0].id],
        elimination_team_ids=[teams.teams[1].id],
        expected_version=champion.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T14:01:00Z"),
    )
    assert repeated_swiss == swiss
    assert shared_event_count() == events_after_swiss


def test_shared_tournament_archive_requires_results_and_can_be_restored(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    contest = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="Архив",
    )
    current = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    with pytest.raises(
        SharedTournamentCompletionUnavailableError,
        match="финальные результаты всех матчей",
    ):
        archive_shared_tournament(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            expected_version=current.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2030-06-01T13:00:00Z"),
        )

    saved_match = save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=match.id,
        home_score=2,
        away_score=1,
        advancing_team_id=shared.teams[0].id,
        expected_version=match.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T13:00:00Z"),
    )
    current = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    archived = archive_shared_tournament(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        expected_version=current.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-06-01T14:00:00Z"),
    )
    assert archived.tournament.is_archived is True
    with pytest.raises(SharedTournamentLockedError, match="архиве"):
        archive_shared_tournament(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            expected_version=archived.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2030-06-01T14:00:00Z"),
        )
    with create_connection(database_path) as connection:
        contest_row = connection.execute(
            "SELECT is_active FROM contests WHERE id = ?", (contest.id,)
        ).fetchone()
    assert contest_row is not None
    assert bool(contest_row["is_active"]) is True
    with pytest.raises(SharedTournamentLockedError, match="архиве"):
        save_shared_match_result(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            shared_match_id=match.id,
            home_score=1,
            away_score=0,
            advancing_team_id=shared.teams[0].id,
            expected_version=saved_match.version,
            actor_telegram_user_id=OWNER_ID,
            actor_first_name="Eugene",
            actor_last_name="Sabir",
            actor_username="evsab",
            now_utc=_time("2030-06-01T15:00:00Z"),
        )

    restored = restore_shared_tournament(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        expected_version=archived.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )
    assert restored.tournament.is_archived is False
    with pytest.raises(SharedTournamentConflictError, match="уже активен"):
        restore_shared_tournament(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            expected_version=restored.tournament.version,
            actor_telegram_user_id=OWNER_ID,
        )
    corrected = save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=match.id,
        home_score=1,
        away_score=0,
        advancing_team_id=shared.teams[0].id,
        expected_version=saved_match.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T15:00:00Z"),
    )
    assert corrected.home_score == 1
    with create_connection(database_path) as connection:
        assert [
            str(row["event_type"])
            for row in connection.execute(
                """
                SELECT event_type
                FROM shared_tournament_events
                WHERE event_type IN (
                    'shared_tournament.archived',
                    'shared_tournament.restored'
                )
                ORDER BY id
                """
            )
        ] == ["shared_tournament.archived", "shared_tournament.restored"]


def test_shared_tournament_restore_rejects_completed_linked_contest(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "completed-linked-contest.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    contest = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="Завершён",
    )
    archived = archive_shared_tournament(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-06-01T14:00:00Z"),
    )
    complete_contest(
        database_path=database_path,
        telegram_chat_id=-1001,
        contest_id=contest.id,
        telegram_user_id=OWNER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        audit_actor=AuditActor(
            telegram_chat_id=-1001,
            telegram_user_id=OWNER_ID,
            role=AuditActorRole.TELEGRAM_ADMIN,
        ),
        now_utc=_time("2030-06-01T15:00:00Z"),
    )

    with pytest.raises(
        SharedTournamentConflictError,
        match="связанных конкурсов уже завершён",
    ):
        restore_shared_tournament(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            expected_version=archived.tournament.version,
            actor_telegram_user_id=OWNER_ID,
        )

    current = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    assert current.tournament.is_archived is True
    assert current.tournament.version == archived.tournament.version


def test_shared_tournament_archive_requires_long_term_results(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    shared = save_shared_champion_settings(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        enabled=True,
        deadline_at="2030-05-01T12:00:00Z",
        points=7,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    shared = save_shared_swiss_settings(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        enabled=True,
        deadline_at="2030-05-01T12:00:00Z",
        direct_qualifier_count=1,
        elimination_qualifier_count=1,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    with pytest.raises(
        SharedTournamentCompletionUnavailableError,
        match="фактического чемпиона",
    ):
        archive_shared_tournament(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            expected_version=shared.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2030-05-02T00:00:00Z"),
        )

    shared = save_shared_champion_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        champion_team_id=shared.teams[0].id,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-05-02T00:00:00Z"),
    )
    with pytest.raises(
        SharedTournamentCompletionUnavailableError,
        match="фактические итоги швейцарского этапа",
    ):
        archive_shared_tournament(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            expected_version=shared.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2030-05-02T00:00:00Z"),
        )

    shared = save_shared_swiss_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        direct_team_ids=[shared.teams[0].id],
        elimination_team_ids=[shared.teams[1].id],
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-05-02T00:00:00Z"),
    )
    archived = archive_shared_tournament(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-05-02T00:00:00Z"),
    )
    assert archived.tournament.is_archived is True


def test_shared_match_is_materialized_per_contest_and_predictions_stay_separate(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    first = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="A",
    )
    second = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1002,
        suffix="B",
    )
    local_ids = _local_match_ids(database_path, match.id)

    assert set(local_ids) == {first.id, second.id}
    assert local_ids[first.id] != local_ids[second.id]

    for contest, chat_id, score in (
        (first, -1001, (2, 1)),
        (second, -1002, (1, 0)),
    ):
        save_match_prediction(
            database_path=database_path,
            telegram_chat_id=chat_id,
            contest_id=contest.id,
            match_id=local_ids[contest.id],
            telegram_user_id=777,
            first_name="Участник",
            last_name=None,
            username=None,
            predicted_home_score=score[0],
            predicted_away_score=score[1],
            predicted_advancing_team_id=shared.teams[0].id,
            now_utc=_time("2030-05-01T00:00:00Z"),
        )

    with create_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT match_id, predicted_home_score, predicted_away_score
            FROM match_predictions
            ORDER BY match_id
            """
        ).fetchall()
    assert [
        (row["predicted_home_score"], row["predicted_away_score"]) for row in rows
    ] == [
        (2, 1),
        (1, 0),
    ]


def test_shared_start_update_is_atomic_and_deadline_never_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    first = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="A",
    )
    second = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1002,
        suffix="B",
    )

    updated = update_shared_match_start(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=match.id,
        starts_at_utc="2030-06-02T15:00:00Z",
        expected_version=match.version,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-05-01T00:00:00Z"),
    )
    with create_connection(database_path) as connection:
        local_times = {
            str(row["starts_at_utc"])
            for row in connection.execute(
                """
                SELECT matches.starts_at_utc
                FROM shared_match_links AS link
                JOIN matches ON matches.id = link.match_id
                WHERE link.shared_match_id = ?
                """,
                (match.id,),
            )
        }
    assert local_times == {"2030-06-02T15:00:00Z"}

    with pytest.raises(SharedMatchUpdateUnavailableError, match="уже наступил"):
        update_shared_match_start(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            shared_match_id=match.id,
            starts_at_utc="2030-06-04T15:00:00Z",
            expected_version=updated.version,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2030-06-03T00:00:00Z"),
        )

    local_ids = _local_match_ids(database_path, match.id)
    with pytest.raises(SharedTournamentManagedError):
        update_match_start(
            database_path=database_path,
            telegram_chat_id=-1001,
            contest_id=first.id,
            match_id=local_ids[first.id],
            telegram_user_id=OWNER_ID,
            first_name="Eugene",
            last_name="Sabir",
            username="evsab",
            starts_at_utc="2030-06-05T00:00:00Z",
            audit_actor=AuditActor(
                telegram_chat_id=-1001,
                telegram_user_id=OWNER_ID,
                role=AuditActorRole.TELEGRAM_ADMIN,
            ),
            now_utc=_time("2030-05-01T00:00:00Z"),
        )
    assert second.id in local_ids


def test_shared_start_update_checks_deadline_after_acquiring_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "shared-match-deadline-race.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-05-01T00:00:00Z"),
    )
    contender_connected = Event()
    clock = {"now": _time("2030-06-01T11:59:59Z")}
    original_database_connection = shared_tournament_service.database_connection

    @contextmanager
    def coordinated_database_connection(path: Path):
        with original_database_connection(path) as connection:
            contender_connected.set()
            yield connection

    def controlled_now(_value: datetime | None) -> datetime:
        return clock["now"]

    monkeypatch.setattr(
        shared_tournament_service,
        "database_connection",
        coordinated_database_connection,
    )
    monkeypatch.setattr(shared_tournament_service, "_resolve_now", controlled_now)

    with create_connection(database_path) as lock_connection:
        lock_connection.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            update_future = executor.submit(
                update_shared_match_start,
                database_path=database_path,
                shared_tournament_id=shared.tournament.id,
                shared_match_id=match.id,
                starts_at_utc="2030-06-02T12:00:00Z",
                expected_version=match.version,
                actor_telegram_user_id=OWNER_ID,
            )
            assert contender_connected.wait(timeout=5)
            clock["now"] = _time("2030-06-01T12:00:00Z")
            lock_connection.execute("COMMIT")
            with pytest.raises(
                SharedMatchUpdateUnavailableError,
                match="Дедлайн матча уже наступил",
            ):
                update_future.result(timeout=5)

    unchanged = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    assert unchanged.matches[0].starts_at_utc == "2030-06-01T12:00:00Z"
    assert unchanged.matches[0].version == match.version


def test_shared_result_recalculates_every_contest_and_correction_changes_scores(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    contests = [
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1001,
                suffix="A",
            ),
            -1001,
        ),
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1002,
                suffix="B",
            ),
            -1002,
        ),
    ]
    local_ids = _local_match_ids(database_path, match.id)
    for contest, chat_id in contests:
        save_match_prediction_publication_settings(
            database_path=database_path,
            telegram_chat_id=chat_id,
            contest_id=contest.id,
            telegram_user_id=OWNER_ID,
            first_name="Eugene",
            last_name="Sabir",
            username="evsab",
            enabled=True,
            now_utc=_time("2030-05-01T00:00:00Z"),
            audit_actor=AuditActor(
                telegram_chat_id=chat_id,
                telegram_user_id=OWNER_ID,
                role=AuditActorRole.TELEGRAM_ADMIN,
            ),
        )
        save_match_prediction(
            database_path=database_path,
            telegram_chat_id=chat_id,
            contest_id=contest.id,
            match_id=local_ids[contest.id],
            telegram_user_id=777,
            first_name="Участник",
            last_name=None,
            username=None,
            predicted_home_score=2,
            predicted_away_score=1,
            predicted_advancing_team_id=shared.teams[0].id,
            now_utc=_time("2030-05-01T00:00:00Z"),
        )

    saved = save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=match.id,
        home_score=2,
        away_score=1,
        advancing_team_id=shared.teams[0].id,
        expected_version=match.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T13:00:00Z"),
    )

    def result_side_effects() -> tuple[int, int, tuple[tuple[object, ...], ...]]:
        with create_connection(database_path) as connection:
            shared_event_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM shared_tournament_events
                    WHERE shared_tournament_id = ?
                      AND event_type IN (
                          'shared_match.result_recorded',
                          'shared_match.result_corrected'
                      )
                    """,
                    (shared.tournament.id,),
                ).fetchone()[0]
            )
            local_event_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM event_log
                    WHERE event_type IN (
                        'shared_match.result_recorded',
                        'shared_match.result_corrected'
                    )
                    """
                ).fetchone()[0]
            )
            publications = connection.execute(
                """
                SELECT contest_id, desired_revision, latest_event_id, updated_at
                FROM contest_publications
                WHERE publication_type = 'match_result'
                ORDER BY contest_id
                """
            ).fetchall()
        return (
            shared_event_count,
            local_event_count,
            tuple(tuple(row) for row in publications),
        )

    side_effects_before_retry = result_side_effects()
    replayed = save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=match.id,
        home_score=2,
        away_score=1,
        advancing_team_id=shared.teams[0].id,
        expected_version=match.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T13:01:00Z"),
    )
    assert replayed == saved
    assert result_side_effects() == side_effects_before_retry

    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT SUM(points) FROM match_prediction_scores"
            ).fetchone()[0]
            == 6
        )

    save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=match.id,
        home_score=0,
        away_score=1,
        advancing_team_id=shared.teams[1].id,
        expected_version=saved.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T14:00:00Z"),
    )
    with create_connection(database_path) as connection:
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


def test_shared_delete_removes_all_local_predictions_and_recreation_is_new_match(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    contest = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="A",
    )
    local_match_id = _local_match_ids(database_path, match.id)[contest.id]
    save_match_prediction(
        database_path=database_path,
        telegram_chat_id=-1001,
        contest_id=contest.id,
        match_id=local_match_id,
        telegram_user_id=777,
        first_name="Участник",
        last_name=None,
        username=None,
        predicted_home_score=1,
        predicted_away_score=0,
        predicted_advancing_team_id=shared.teams[0].id,
        now_utc=_time("2030-05-01T00:00:00Z"),
    )

    deletion = delete_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=match.id,
        expected_version=match.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
    )
    assert deletion.linked_contest_count == 1
    assert deletion.deleted_prediction_count == 1

    recreated = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-07-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-06-02T00:00:00Z"),
    )
    assert recreated.id != match.id
    with create_connection(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM match_predictions").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM shared_match_links").fetchone()[0]
            == 1
        )


def test_same_team_pair_is_unique_regardless_of_time_or_order(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    with pytest.raises(SharedMatchConflictError):
        create_shared_match(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            home_team_id=shared.teams[1].id,
            away_team_id=shared.teams[0].id,
            starts_at_utc="2030-08-01T12:00:00Z",
            best_of=None,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2029-01-01T00:00:00Z"),
        )


def test_shared_champion_settings_and_corrections_reach_completed_contests(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    shared = save_shared_champion_settings(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        enabled=True,
        deadline_at="2030-06-01T12:00:00Z",
        points=7,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    with pytest.raises(SharedTournamentConflictError, match="уже был изменён"):
        save_shared_champion_settings(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            enabled=True,
            deadline_at="2030-06-02T12:00:00Z",
            points=5,
            expected_version=shared.tournament.version - 1,
            actor_telegram_user_id=OWNER_ID,
            actor_first_name="Eugene",
            actor_last_name="Sabir",
            actor_username="evsab",
            now_utc=_time("2029-01-01T00:00:00Z"),
        )
    contests = [
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1001,
                suffix="A",
            ),
            -1001,
        ),
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1002,
                suffix="B",
            ),
            -1002,
        ),
    ]
    for contest, chat_id in contests:
        save_champion_prediction(
            database_path=database_path,
            telegram_chat_id=chat_id,
            contest_id=contest.id,
            telegram_user_id=777,
            first_name="Участник",
            last_name=None,
            username=None,
            predicted_team_id=shared.teams[0].id,
            now_utc=_time("2030-05-01T00:00:00Z"),
        )
    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET is_active = 0 WHERE id = ?", (contests[1][0].id,)
        )

    saved = save_shared_champion_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        champion_team_id=shared.teams[0].id,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-02T00:00:00Z"),
    )
    assert saved.champion_prediction.actual_champion == shared.teams[0]
    corrected = save_shared_champion_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        champion_team_id=shared.teams[1].id,
        expected_version=saved.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-03T00:00:00Z"),
    )
    assert corrected.champion_prediction.actual_champion == shared.teams[1]
    with create_connection(database_path) as connection:
        assert {
            int(row["champion_team_id"])
            for row in connection.execute("SELECT champion_team_id FROM contests")
        } == {shared.teams[1].id}


def test_shared_swiss_result_is_common_but_predictions_are_per_contest(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    shared = save_shared_swiss_settings(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        enabled=True,
        deadline_at="2030-06-01T12:00:00Z",
        direct_qualifier_count=1,
        elimination_qualifier_count=1,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    contests = [
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1001,
                suffix="A",
            ),
            -1001,
        ),
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1002,
                suffix="B",
            ),
            -1002,
        ),
    ]
    for contest, chat_id in contests:
        save_swiss_stage_prediction(
            database_path=database_path,
            telegram_chat_id=chat_id,
            contest_id=contest.id,
            telegram_user_id=777,
            first_name="Участник",
            last_name=None,
            username=None,
            direct_team_ids=[shared.teams[0].id],
            elimination_team_ids=[shared.teams[1].id],
            now_utc=_time("2030-05-01T00:00:00Z"),
        )
    saved = save_shared_swiss_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        direct_team_ids=[shared.teams[0].id],
        elimination_team_ids=[shared.teams[1].id],
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-02T00:00:00Z"),
    )
    assert saved.swiss_stage_prediction.direct_qualifier_team_ids == (
        shared.teams[0].id,
    )
    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM swiss_stage_predictions"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM swiss_stage_results").fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM swiss_stage_result_selections"
            ).fetchone()[0]
            == 4
        )


def test_shared_deadline_only_update_preserves_locked_custom_swiss_policy(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "custom-policy.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    shared = save_shared_swiss_settings(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        enabled=True,
        deadline_at="2030-06-01T12:00:00Z",
        direct_qualifier_count=1,
        elimination_qualifier_count=1,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    contest = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="custom-policy",
    )
    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE shared_tournament_settings
            SET swiss_selection_mode = 'up_to_limits',
                swiss_direct_correct_points = 7,
                swiss_elimination_correct_points = 3,
                swiss_cross_category_points = 2
            WHERE shared_tournament_id = ?
            """,
            (shared.tournament.id,),
        )
        connection.execute(
            """
            UPDATE swiss_stage_prediction_settings
            SET selection_mode = 'up_to_limits',
                direct_correct_points = 7,
                elimination_correct_points = 3,
                cross_category_points = 2
            WHERE contest_id = ?
            """,
            (contest.id,),
        )
    save_swiss_stage_prediction(
        database_path=database_path,
        telegram_chat_id=-1001,
        contest_id=contest.id,
        telegram_user_id=777,
        first_name="Участник",
        last_name=None,
        username=None,
        direct_team_ids=[shared.teams[0].id],
        elimination_team_ids=[],
        now_utc=_time("2030-05-01T00:00:00Z"),
    )
    shared = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )

    updated = save_shared_swiss_settings(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        enabled=True,
        deadline_at="2030-06-02T12:00:00Z",
        direct_qualifier_count=1,
        elimination_qualifier_count=1,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-05-01T00:00:00Z"),
    )

    assert updated.swiss_stage_prediction.selection_mode == "up_to_limits"
    assert updated.swiss_stage_prediction.direct_correct_points == 7
    assert updated.swiss_stage_prediction.elimination_correct_points == 3
    assert updated.swiss_stage_prediction.cross_category_points == 2
    assert updated.swiss_stage_prediction.maximum_points == 10
    with create_connection(database_path) as connection:
        local_policy = connection.execute(
            """
            SELECT selection_mode, direct_correct_points,
                   elimination_correct_points, cross_category_points
            FROM swiss_stage_prediction_settings WHERE contest_id = ?
            """,
            (contest.id,),
        ).fetchone()
    assert tuple(local_policy) == ("up_to_limits", 7, 3, 2)
