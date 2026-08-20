from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    create_match,
    create_world_cup_2026_contest,
    save_tournament_teams,
)
from app.database import create_connection, initialize_database
from scripts.migrate_shared_tournaments import (
    analyze_database,
    migrate_database,
)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _create_contest_and_match(
    database_path: Path,
    *,
    chat_id: int,
    suffix: str,
    starts_at_utc: str,
) -> tuple[int, int]:
    actor = AuditActor(
        telegram_chat_id=chat_id,
        telegram_user_id=123,
        role=AuditActorRole.TELEGRAM_ADMIN,
    )
    contest = create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=chat_id,
        chat_title=f"Чат {suffix}",
        telegram_user_id=123,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name=f"Конкурс {suffix}",
        idempotency_key=f"contest-{suffix}",
        audit_actor=actor,
    ).contest
    teams = save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=chat_id,
        contest_id=contest.id,
        team_names=["Испания", "Франция"],
        audit_actor=actor,
    ).teams
    match = create_match(
        database_path=database_path,
        telegram_chat_id=chat_id,
        contest_id=contest.id,
        telegram_user_id=123,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        home_team_id=teams[0].id,
        away_team_id=teams[1].id,
        starts_at_utc=starts_at_utc,
        idempotency_key=f"match-{suffix}",
        audit_actor=actor,
    ).match
    return contest.id, match.id


def test_migration_groups_same_teams_and_uses_latest_future_time(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    _create_contest_and_match(
        database_path,
        chat_id=-1001,
        suffix="A",
        starts_at_utc="2030-06-01T12:00:00Z",
    )
    _create_contest_and_match(
        database_path,
        chat_id=-1002,
        suffix="B",
        starts_at_utc="2030-06-02T12:00:00Z",
    )

    plan = analyze_database(database_path, now_utc=_time("2029-01-01T00:00:00Z"))
    assert plan.contest_count == 2
    assert plan.match_group_count == 1
    assert len(plan.differing_time_groups) == 1
    assert plan.conflicts == ()

    migrate_database(
        database_path,
        actor_telegram_user_id=123,
        time_policy="latest",
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    with create_connection(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM shared_tournaments").fetchone()[0]
            == 1
        )
        shared_match = connection.execute(
            "SELECT starts_at_utc FROM shared_matches"
        ).fetchone()
        assert shared_match["starts_at_utc"] == "2030-06-02T12:00:00Z"
        assert (
            connection.execute("SELECT COUNT(*) FROM shared_match_links").fetchone()[0]
            == 2
        )
        local_times = {
            str(row["starts_at_utc"])
            for row in connection.execute("SELECT starts_at_utc FROM matches")
        }
        assert local_times == {"2030-06-02T12:00:00Z"}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_preserves_elapsed_local_deadlines_and_uses_latest_shared_time(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    _create_contest_and_match(
        database_path,
        chat_id=-1001,
        suffix="A",
        starts_at_utc="2029-06-01T12:00:00Z",
    )
    _create_contest_and_match(
        database_path,
        chat_id=-1002,
        suffix="B",
        starts_at_utc="2029-06-02T12:00:00Z",
    )

    plan = analyze_database(database_path, now_utc=_time("2030-01-01T00:00:00Z"))
    assert plan.conflicts == ()
    migrate_database(
        database_path,
        actor_telegram_user_id=123,
        now_utc=_time("2030-01-01T00:00:00Z"),
    )

    with create_connection(database_path) as connection:
        assert (
            connection.execute("SELECT starts_at_utc FROM shared_matches").fetchone()[0]
            == "2029-06-02T12:00:00Z"
        )
        assert {
            str(row["starts_at_utc"])
            for row in connection.execute("SELECT starts_at_utc FROM matches")
        } == {"2029-06-01T12:00:00Z", "2029-06-02T12:00:00Z"}


def test_migration_refuses_partially_present_matches(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    _create_contest_and_match(
        database_path,
        chat_id=-1001,
        suffix="A",
        starts_at_utc="2030-06-01T12:00:00Z",
    )
    actor = AuditActor(
        telegram_chat_id=-1002,
        telegram_user_id=123,
        role=AuditActorRole.TELEGRAM_ADMIN,
    )
    second = create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=-1002,
        chat_title="Чат B",
        telegram_user_id=123,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name="Конкурс B",
        idempotency_key="contest-B",
        audit_actor=actor,
    ).contest
    save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=-1002,
        contest_id=second.id,
        team_names=["Испания", "Франция"],
        audit_actor=actor,
    )

    plan = analyze_database(database_path, now_utc=_time("2029-01-01T00:00:00Z"))

    assert any("не во всех конкурсах" in conflict for conflict in plan.conflicts)


def test_migration_preserves_per_contest_champion_points(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    first_contest_id, _ = _create_contest_and_match(
        database_path,
        chat_id=-1001,
        suffix="A",
        starts_at_utc="2030-06-01T12:00:00Z",
    )
    _create_contest_and_match(
        database_path,
        chat_id=-1002,
        suffix="B",
        starts_at_utc="2030-06-01T12:00:00Z",
    )
    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET champion_prediction_points = 9 WHERE id = ?",
            (first_contest_id,),
        )

    plan = analyze_database(database_path, now_utc=_time("2029-01-01T00:00:00Z"))

    assert plan.conflicts == ()
    migrate_database(
        database_path,
        actor_telegram_user_id=123,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    with create_connection(database_path) as connection:
        assert {
            int(row["champion_prediction_points"])
            for row in connection.execute(
                "SELECT champion_prediction_points FROM contests"
            )
        } == {5, 9}


def test_migration_leaves_completed_contests_independent(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    completed_contest_id, _ = _create_contest_and_match(
        database_path,
        chat_id=-1001,
        suffix="completed",
        starts_at_utc="2029-06-01T12:00:00Z",
    )
    active_contest_id, _ = _create_contest_and_match(
        database_path,
        chat_id=-1002,
        suffix="active",
        starts_at_utc="2030-06-01T12:00:00Z",
    )
    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET is_active = 0 WHERE id = ?",
            (completed_contest_id,),
        )

    plan = analyze_database(database_path, now_utc=_time("2029-01-01T00:00:00Z"))
    assert plan.contest_count == 1
    assert plan.conflicts == ()
    migrate_database(
        database_path,
        actor_telegram_user_id=123,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    with create_connection(database_path) as connection:
        linked_ids = {
            int(row["contest_id"])
            for row in connection.execute(
                "SELECT contest_id FROM contest_shared_tournaments"
            )
        }
    assert linked_ids == {active_contest_id}
