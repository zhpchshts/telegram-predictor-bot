from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    create_champions_league_2026_27_contest,
    create_match,
    create_world_cup_2026_contest,
    save_tournament_teams,
)
from app.database import create_connection, initialize_database
from scripts.migrate_general_stage_prediction import (
    migrate_database as migrate_general_stage_database,
)
from scripts.migrate_shared_tournaments import (
    SharedTournamentMigrationConflictError,
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


def _downgrade_general_stage_policy_and_remove_shared_schema(
    database_path: Path,
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            CREATE TABLE swiss_stage_prediction_settings__legacy (
                contest_id INTEGER PRIMARY KEY
                    REFERENCES contests(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                deadline_at TEXT,
                direct_qualifier_count INTEGER NOT NULL DEFAULT 3
                    CHECK (direct_qualifier_count > 0),
                elimination_qualifier_count INTEGER NOT NULL DEFAULT 5
                    CHECK (elimination_qualifier_count > 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO swiss_stage_prediction_settings__legacy (
                contest_id, enabled, deadline_at,
                direct_qualifier_count, elimination_qualifier_count,
                created_at, updated_at
            )
            SELECT contest_id, enabled, deadline_at,
                   direct_qualifier_count, elimination_qualifier_count,
                   created_at, updated_at
            FROM swiss_stage_prediction_settings;
            DROP TABLE swiss_stage_prediction_settings;
            ALTER TABLE swiss_stage_prediction_settings__legacy
                RENAME TO swiss_stage_prediction_settings;

            DROP TABLE shared_tournament_events;
            DROP TABLE shared_tie_links;
            DROP TABLE shared_match_links;
            DROP TABLE contest_shared_tournaments;
            DROP TABLE shared_match_external_links;
            DROP TABLE shared_matches;
            DROP TABLE shared_two_legged_ties;
            DROP TABLE shared_swiss_stage_result_selections;
            DROP TABLE shared_tournament_teams;
            DROP TABLE shared_tournament_settings;
            DROP TABLE shared_tournaments;
            """
        )


def _downgrade_empty_shared_policy_schema(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            CREATE TABLE shared_tournament_settings__legacy (
                shared_tournament_id INTEGER PRIMARY KEY
                    REFERENCES shared_tournaments(id) ON DELETE CASCADE,
                champion_prediction_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK (champion_prediction_enabled IN (0, 1)),
                champion_prediction_deadline_at TEXT,
                champion_prediction_points INTEGER NOT NULL DEFAULT 5
                    CHECK (champion_prediction_points >= 0),
                champion_team_id INTEGER REFERENCES teams(id),
                swiss_stage_prediction_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK (swiss_stage_prediction_enabled IN (0, 1)),
                swiss_stage_prediction_deadline_at TEXT,
                swiss_direct_qualifier_count INTEGER NOT NULL DEFAULT 3
                    CHECK (swiss_direct_qualifier_count > 0),
                swiss_elimination_qualifier_count INTEGER NOT NULL DEFAULT 5
                    CHECK (swiss_elimination_qualifier_count > 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO shared_tournament_settings__legacy (
                shared_tournament_id, champion_prediction_enabled,
                champion_prediction_deadline_at, champion_prediction_points,
                champion_team_id, swiss_stage_prediction_enabled,
                swiss_stage_prediction_deadline_at,
                swiss_direct_qualifier_count,
                swiss_elimination_qualifier_count, created_at, updated_at
            )
            SELECT shared_tournament_id, champion_prediction_enabled,
                   champion_prediction_deadline_at, champion_prediction_points,
                   champion_team_id, swiss_stage_prediction_enabled,
                   swiss_stage_prediction_deadline_at,
                   swiss_direct_qualifier_count,
                   swiss_elimination_qualifier_count, created_at, updated_at
            FROM shared_tournament_settings;
            DROP TABLE shared_tournament_settings;
            ALTER TABLE shared_tournament_settings__legacy
                RENAME TO shared_tournament_settings;
            """
        )


def test_migration_bootstraps_shared_schema_from_legacy_local_policy(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-policy.db"
    initialize_database(database_path)
    actor = AuditActor(
        telegram_chat_id=-1001,
        telegram_user_id=123,
        role=AuditActorRole.TELEGRAM_ADMIN,
    )
    contest = create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=-1001,
        chat_title="Лига чемпионов",
        telegram_user_id=123,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name="Конкурс ЛЧ",
        idempotency_key="legacy-ucl",
        audit_actor=actor,
    ).contest
    save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=-1001,
        contest_id=contest.id,
        team_names=[f"Команда {number:02d}" for number in range(1, 37)],
        audit_actor=actor,
    )
    with create_connection(database_path) as connection:
        timestamps_before = tuple(
            connection.execute(
                """
                SELECT created_at, updated_at
                FROM swiss_stage_prediction_settings WHERE contest_id = ?
                """,
                (contest.id,),
            ).fetchone()
        )
    _downgrade_general_stage_policy_and_remove_shared_schema(database_path)

    plan = analyze_database(database_path, now_utc=_time("2029-01-01T00:00:00Z"))
    assert plan.conflicts == ()
    migrate_database(
        database_path,
        actor_telegram_user_id=123,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )

    with create_connection(database_path) as connection:
        local = connection.execute(
            """
            SELECT selection_mode, direct_correct_points,
                   elimination_correct_points, cross_category_points,
                   created_at, updated_at
            FROM swiss_stage_prediction_settings WHERE contest_id = ?
            """,
            (contest.id,),
        ).fetchone()
        shared = connection.execute(
            """
            SELECT swiss_selection_mode, swiss_direct_correct_points,
                   swiss_elimination_correct_points,
                   swiss_cross_category_points
            FROM shared_tournament_settings
            """
        ).fetchone()
        link_count = connection.execute(
            "SELECT COUNT(*) FROM contest_shared_tournaments"
        ).fetchone()[0]

    assert tuple(local[:4]) == ("up_to_limits", 2, 1, 0)
    assert local[4] == timestamps_before[0]
    assert tuple(shared) == ("up_to_limits", 2, 1, 0)
    assert link_count == 1

    report = migrate_general_stage_database(
        database_path,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    assert report.champions_league_contest_count == 1
    assert report.champions_league_shared_tournament_count == 1


def test_migration_adds_policy_to_existing_empty_legacy_shared_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-shared-policy.db"
    initialize_database(database_path)
    actor = AuditActor(
        telegram_chat_id=-1001,
        telegram_user_id=123,
        role=AuditActorRole.TELEGRAM_ADMIN,
    )
    contest = create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=-1001,
        chat_title="Лига чемпионов",
        telegram_user_id=123,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name="Конкурс ЛЧ",
        idempotency_key="legacy-shared-ucl",
        audit_actor=actor,
    ).contest
    save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=-1001,
        contest_id=contest.id,
        team_names=[f"Команда {number:02d}" for number in range(1, 37)],
        audit_actor=actor,
    )
    _downgrade_empty_shared_policy_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shared_tournament_settings"
            ).fetchone()[0]
            == 0
        )

    migrate_database(
        database_path,
        actor_telegram_user_id=123,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )

    with create_connection(database_path) as connection:
        shared = connection.execute(
            """
            SELECT swiss_selection_mode, swiss_direct_correct_points,
                   swiss_elimination_correct_points,
                   swiss_cross_category_points
            FROM shared_tournament_settings
            """
        ).fetchone()
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(shared_tournament_settings)"
            )
        }
    assert tuple(shared) == ("up_to_limits", 2, 1, 0)
    assert {
        "swiss_selection_mode",
        "swiss_direct_correct_points",
        "swiss_elimination_correct_points",
        "swiss_cross_category_points",
    }.issubset(columns)


def test_migration_refuses_partial_local_policy_before_shared_schema_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "partial-policy.db"
    initialize_database(database_path)
    _downgrade_general_stage_policy_and_remove_shared_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            ALTER TABLE swiss_stage_prediction_settings
            ADD COLUMN selection_mode TEXT NOT NULL DEFAULT 'exact'
                CHECK (selection_mode IN ('exact', 'up_to_limits'))
            """
        )

    with pytest.raises(
        SharedTournamentMigrationConflictError,
        match="Partially applied general-stage policy schema",
    ):
        migrate_database(
            database_path,
            actor_telegram_user_id=123,
            now_utc=_time("2029-01-01T00:00:00Z"),
        )

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(swiss_stage_prediction_settings)"
            )
        }
        shared_table_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'shared_tournaments'
            """
        ).fetchone()
    assert columns.intersection(
        {
            "selection_mode",
            "direct_correct_points",
            "elimination_correct_points",
            "cross_category_points",
        }
    ) == {"selection_mode"}
    assert shared_table_exists is None


def test_migration_refuses_partial_shared_policy_before_any_new_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "partial-shared-policy.db"
    initialize_database(database_path)
    _downgrade_empty_shared_policy_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            ALTER TABLE shared_tournament_settings
            ADD COLUMN swiss_selection_mode TEXT NOT NULL DEFAULT 'exact'
                CHECK (swiss_selection_mode IN ('exact', 'up_to_limits'))
            """
        )

    with pytest.raises(
        SharedTournamentMigrationConflictError,
        match="Partially applied shared general-stage policy schema",
    ):
        migrate_database(
            database_path,
            actor_telegram_user_id=123,
            now_utc=_time("2029-01-01T00:00:00Z"),
        )

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(shared_tournament_settings)"
            )
        }
    assert columns.intersection(
        {
            "swiss_selection_mode",
            "swiss_direct_correct_points",
            "swiss_elimination_correct_points",
            "swiss_cross_category_points",
        }
    ) == {"swiss_selection_mode"}


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
        shared_policy = connection.execute(
            """
            SELECT swiss_selection_mode,
                   swiss_direct_correct_points,
                   swiss_elimination_correct_points,
                   swiss_cross_category_points
            FROM shared_tournament_settings
            """
        ).fetchone()
        local_policies = {
            tuple(row)
            for row in connection.execute(
                """
                SELECT selection_mode, direct_correct_points,
                       elimination_correct_points, cross_category_points
                FROM swiss_stage_prediction_settings
                """
            )
        }
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
    assert tuple(shared_policy) == ("exact", 2, 2, 1)
    assert local_policies == {("exact", 2, 2, 1)}


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


def test_migration_does_not_start_future_shared_match_from_one_elapsed_deadline(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    first_contest_id, first_match_id = _create_contest_and_match(
        database_path,
        chat_id=-1001,
        suffix="A",
        starts_at_utc="2030-06-01T08:00:00Z",
    )
    second_contest_id, second_match_id = _create_contest_and_match(
        database_path,
        chat_id=-1002,
        suffix="B",
        starts_at_utc="2030-06-01T10:00:00Z",
    )

    migrate_database(
        database_path,
        actor_telegram_user_id=123,
        now_utc=_time("2030-06-01T09:00:00Z"),
    )

    with create_connection(database_path) as connection:
        shared = connection.execute(
            "SELECT starts_at_utc, status FROM shared_matches"
        ).fetchone()
        local_rows = {
            int(row["id"]): (str(row["starts_at_utc"]), str(row["status"]))
            for row in connection.execute(
                "SELECT id, starts_at_utc, status FROM matches"
            )
        }
        links = {
            int(row["contest_id"])
            for row in connection.execute(
                "SELECT contest_id FROM contest_shared_tournaments"
            )
        }
    assert tuple(shared) == ("2030-06-01T10:00:00Z", "scheduled")
    assert local_rows[first_match_id] == ("2030-06-01T08:00:00Z", "started")
    assert local_rows[second_match_id] == ("2030-06-01T10:00:00Z", "scheduled")
    assert links == {first_contest_id, second_contest_id}


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
