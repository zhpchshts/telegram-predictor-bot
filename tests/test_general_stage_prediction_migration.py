from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from app.database import initialize_database
from scripts.migrate_general_stage_prediction import (
    backup_database,
    migrate_database,
)


LEGACY_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE teams (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE contests (
    id INTEGER PRIMARY KEY,
    template_key TEXT NOT NULL
);
CREATE TABLE contest_teams (
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    position INTEGER NOT NULL,
    PRIMARY KEY (contest_id, team_id),
    UNIQUE (contest_id, position)
);
CREATE TABLE swiss_stage_prediction_settings (
    contest_id INTEGER PRIMARY KEY REFERENCES contests(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0,
    deadline_at TEXT,
    direct_qualifier_count INTEGER NOT NULL DEFAULT 3,
    elimination_qualifier_count INTEGER NOT NULL DEFAULT 5,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE swiss_stage_predictions (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, contest_id),
    UNIQUE (contest_id, user_id)
);
CREATE TABLE swiss_stage_prediction_selections (
    prediction_id INTEGER NOT NULL,
    contest_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    category TEXT NOT NULL,
    PRIMARY KEY (prediction_id, team_id),
    FOREIGN KEY (prediction_id, contest_id)
        REFERENCES swiss_stage_predictions(id, contest_id) ON DELETE CASCADE
);
CREATE TABLE swiss_stage_results (
    contest_id INTEGER PRIMARY KEY REFERENCES contests(id) ON DELETE CASCADE,
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE swiss_stage_result_selections (
    contest_id INTEGER NOT NULL REFERENCES swiss_stage_results(contest_id)
        ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    category TEXT NOT NULL,
    PRIMARY KEY (contest_id, team_id)
);
CREATE TABLE shared_tournaments (
    id INTEGER PRIMARY KEY,
    template_key TEXT NOT NULL
);
CREATE TABLE shared_tournament_settings (
    shared_tournament_id INTEGER PRIMARY KEY
        REFERENCES shared_tournaments(id) ON DELETE CASCADE,
    swiss_stage_prediction_enabled INTEGER NOT NULL DEFAULT 0,
    swiss_stage_prediction_deadline_at TEXT,
    swiss_direct_qualifier_count INTEGER NOT NULL DEFAULT 3,
    swiss_elimination_qualifier_count INTEGER NOT NULL DEFAULT 5,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE shared_tournament_teams (
    shared_tournament_id INTEGER NOT NULL
        REFERENCES shared_tournaments(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    position INTEGER NOT NULL,
    PRIMARY KEY (shared_tournament_id, team_id),
    UNIQUE (shared_tournament_id, position)
);
CREATE TABLE shared_swiss_stage_result_selections (
    shared_tournament_id INTEGER NOT NULL
        REFERENCES shared_tournaments(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    category TEXT NOT NULL,
    PRIMARY KEY (shared_tournament_id, team_id)
);
CREATE TABLE contest_shared_tournaments (
    contest_id INTEGER PRIMARY KEY REFERENCES contests(id) ON DELETE CASCADE,
    shared_tournament_id INTEGER NOT NULL
        REFERENCES shared_tournaments(id) ON DELETE RESTRICT
);
CREATE TABLE event_log (
    id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _create_legacy_database(
    database_path: Path,
    *,
    elimination_limit: int = 12,
    linked_deadline: str = "2030-09-01T12:00:00Z",
    shared_deadline: str = "2030-09-01T12:00:00Z",
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.executemany(
            "INSERT INTO teams (id, name) VALUES (?, ?)",
            [(team_id, f"Команда {team_id:02d}") for team_id in range(1, 37)],
        )
        connection.execute(
            "INSERT INTO contests (id, template_key) VALUES (10, ?)",
            ("champions_league_2026_27",),
        )
        connection.execute(
            "INSERT INTO shared_tournaments (id, template_key) VALUES (20, ?)",
            ("champions_league_2026_27",),
        )
        connection.executemany(
            """
            INSERT INTO contest_teams (contest_id, team_id, position)
            VALUES (10, ?, ?)
            """,
            [(team_id, position) for position, team_id in enumerate(range(1, 37))],
        )
        connection.executemany(
            """
            INSERT INTO shared_tournament_teams (
                shared_tournament_id, team_id, position
            ) VALUES (20, ?, ?)
            """,
            [(team_id, position) for position, team_id in enumerate(range(1, 37))],
        )
        connection.execute(
            """
            INSERT INTO swiss_stage_prediction_settings (
                contest_id, enabled, deadline_at,
                direct_qualifier_count, elimination_qualifier_count,
                created_at, updated_at
            ) VALUES (10, 1, ?, 8, ?, '2026-01-01', '2026-02-01')
            """,
            (linked_deadline, elimination_limit),
        )
        connection.execute(
            """
            INSERT INTO shared_tournament_settings (
                shared_tournament_id, swiss_stage_prediction_enabled,
                swiss_stage_prediction_deadline_at,
                swiss_direct_qualifier_count,
                swiss_elimination_qualifier_count, created_at, updated_at
            ) VALUES (20, 1, ?, 8, ?,
                      '2026-01-02', '2026-02-02')
            """,
            (shared_deadline, elimination_limit),
        )
        connection.execute(
            """
            INSERT INTO contest_shared_tournaments (contest_id, shared_tournament_id)
            VALUES (10, 20)
            """
        )
        connection.execute(
            """
            INSERT INTO event_log (id, payload, created_at)
            VALUES (30, 'unchanged', '2026-03-01')
            """
        )


def _insert_prediction(
    database_path: Path, *, direct_count: int, elimination_count: int
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO swiss_stage_predictions (id, contest_id, user_id) "
            "VALUES (40, 10, 123)"
        )
        connection.executemany(
            """
            INSERT INTO swiss_stage_prediction_selections (
                prediction_id, contest_id, team_id, category
            ) VALUES (40, 10, ?, ?)
            """,
            [
                *[(team_id, "direct") for team_id in range(1, direct_count + 1)],
                *[
                    (team_id, "elimination")
                    for team_id in range(9, 9 + elimination_count)
                ],
            ],
        )


def _insert_exact_linked_result(database_path: Path) -> None:
    selections = [
        *[(team_id, "direct") for team_id in range(1, 9)],
        *[(team_id, "elimination") for team_id in range(9, 21)],
    ]
    with sqlite3.connect(database_path) as connection:
        connection.execute("INSERT INTO swiss_stage_results (contest_id) VALUES (10)")
        connection.executemany(
            """
            INSERT INTO swiss_stage_result_selections (contest_id, team_id, category)
            VALUES (10, ?, ?)
            """,
            selections,
        )
        connection.executemany(
            """
            INSERT INTO shared_swiss_stage_result_selections (
                shared_tournament_id, team_id, category
            ) VALUES (20, ?, ?)
            """,
            selections,
        )


def test_migration_adds_policy_backfills_cl_and_preserves_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_database(database_path)
    _insert_prediction(database_path, direct_count=3, elimination_count=4)
    _insert_exact_linked_result(database_path)

    first_report = migrate_database(database_path)
    second_report = migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        local = connection.execute(
            "SELECT * FROM swiss_stage_prediction_settings WHERE contest_id = 10"
        ).fetchone()
        shared = connection.execute(
            """
            SELECT * FROM shared_tournament_settings
            WHERE shared_tournament_id = 20
            """
        ).fetchone()
        event = connection.execute("SELECT * FROM event_log WHERE id = 30").fetchone()
        prediction_selection_count = connection.execute(
            "SELECT COUNT(*) FROM swiss_stage_prediction_selections"
        ).fetchone()[0]
        result_selection_count = connection.execute(
            "SELECT COUNT(*) FROM swiss_stage_result_selections"
        ).fetchone()[0]
        shared_result_selection_count = connection.execute(
            "SELECT COUNT(*) FROM shared_swiss_stage_result_selections"
        ).fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert (
        local["selection_mode"],
        local["direct_correct_points"],
        local["elimination_correct_points"],
        local["cross_category_points"],
    ) == ("up_to_limits", 2, 1, 0)
    assert (
        shared["swiss_selection_mode"],
        shared["swiss_direct_correct_points"],
        shared["swiss_elimination_correct_points"],
        shared["swiss_cross_category_points"],
    ) == ("up_to_limits", 2, 1, 0)
    assert (local["created_at"], local["updated_at"]) == (
        "2026-01-01",
        "2026-02-01",
    )
    assert (shared["created_at"], shared["updated_at"]) == (
        "2026-01-02",
        "2026-02-02",
    )
    assert tuple(event) == (30, "unchanged", "2026-03-01")
    assert prediction_selection_count == 7
    assert result_selection_count == 20
    assert shared_result_selection_count == 20
    assert first_report.prediction_count == second_report.prediction_count == 1
    assert first_report.local_result_count == second_report.local_result_count == 1
    assert integrity == "ok"
    assert foreign_key_errors == []


def test_migration_normalizes_unlocked_legacy_eight_plus_sixteen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_database(database_path, elimination_limit=16)

    report = migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        local_limit = connection.execute(
            """
            SELECT elimination_qualifier_count
            FROM swiss_stage_prediction_settings WHERE contest_id = 10
            """
        ).fetchone()[0]
        shared_limit = connection.execute(
            """
            SELECT swiss_elimination_qualifier_count
            FROM shared_tournament_settings WHERE shared_tournament_id = 20
            """
        ).fetchone()[0]
    assert (local_limit, shared_limit) == (12, 12)
    assert report.normalized_local_legacy_limit_count == 1
    assert report.normalized_shared_legacy_limit_count == 1


def test_migration_rejects_legacy_eight_plus_sixteen_with_data_atomically(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_database(database_path, elimination_limit=16)
    _insert_prediction(database_path, direct_count=1, elimination_count=1)

    with pytest.raises(RuntimeError, match=r"legacy 8\+16 limits"):
        migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(swiss_stage_prediction_settings)"
            )
        }
        elimination_limit = connection.execute(
            """
            SELECT elimination_qualifier_count
            FROM swiss_stage_prediction_settings WHERE contest_id = 10
            """
        ).fetchone()[0]
    assert "selection_mode" not in columns
    assert elimination_limit == 16


def test_migration_rejects_over_limit_prediction_atomically(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_database(database_path)
    _insert_prediction(database_path, direct_count=9, elimination_count=0)

    with pytest.raises(RuntimeError, match=r"exceeds 8\+12 limits"):
        migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(shared_tournament_settings)"
            )
        }
    assert "swiss_selection_mode" not in columns


def test_migration_rejects_inexact_or_unlinked_result(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("INSERT INTO swiss_stage_results (contest_id) VALUES (10)")
        connection.executemany(
            """
            INSERT INTO swiss_stage_result_selections (contest_id, team_id, category)
            VALUES (10, ?, 'direct')
            """,
            [(team_id,) for team_id in range(1, 9)],
        )

    with pytest.raises(RuntimeError, match=r"not an exact 8\+12 selection"):
        migrate_database(database_path)


def test_migration_rejects_different_exact_linked_result_sets(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_database(database_path)
    _insert_exact_linked_result(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE shared_swiss_stage_result_selections
            SET category = 'elimination'
            WHERE shared_tournament_id = 20 AND team_id = 1
            """
        )
        connection.execute(
            """
            UPDATE shared_swiss_stage_result_selections
            SET category = 'direct'
            WHERE shared_tournament_id = 20 AND team_id = 9
            """
        )

    with pytest.raises(RuntimeError, match="linked general-stage results"):
        migrate_database(database_path)


def test_migration_rejects_linked_settings_mismatch(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_database(
        database_path,
        linked_deadline="2030-09-02T12:00:00Z",
    )

    with pytest.raises(RuntimeError, match="linked open general-stage deadlines"):
        migrate_database(
            database_path,
            now_utc=_time("2029-01-01T00:00:00Z"),
        )


def test_migration_preserves_divergent_elapsed_linked_deadlines(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_database(
        database_path,
        linked_deadline="2029-08-31T12:00:00Z",
        shared_deadline="2029-09-01T12:00:00Z",
    )

    migrate_database(
        database_path,
        now_utc=_time("2030-01-01T00:00:00Z"),
    )

    with sqlite3.connect(database_path) as connection:
        local_deadline = connection.execute(
            """
            SELECT deadline_at FROM swiss_stage_prediction_settings
            WHERE contest_id = 10
            """
        ).fetchone()[0]
        shared_deadline = connection.execute(
            """
            SELECT swiss_stage_prediction_deadline_at
            FROM shared_tournament_settings WHERE shared_tournament_id = 20
            """
        ).fetchone()[0]
    assert (local_deadline, shared_deadline) == (
        "2029-08-31T12:00:00Z",
        "2029-09-01T12:00:00Z",
    )


def test_migration_accepts_fresh_schema_and_backup_is_non_destructive(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "current.db"
    backup_path = tmp_path / "current.backup.db"
    initialize_database(database_path)

    backup_database(database_path, backup_path)
    report = migrate_database(database_path)

    assert report.champions_league_contest_count == 0
    with sqlite3.connect(backup_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(swiss_stage_prediction_settings)"
            )
        }
    assert "selection_mode" in columns
    with pytest.raises(RuntimeError, match="already exists"):
        backup_database(database_path, backup_path)
    with pytest.raises(RuntimeError, match="does not exist"):
        migrate_database(tmp_path / "missing.db")
