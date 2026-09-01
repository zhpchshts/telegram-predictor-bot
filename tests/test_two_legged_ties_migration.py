from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from app.database import initialize_database
from scripts.migrate_two_legged_ties import backup_database, migrate_database


LEGACY_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE teams (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE shared_tournaments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE contests (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE scoring_rule_sets (
    id INTEGER PRIMARY KEY
);
CREATE TABLE stages (
    id INTEGER PRIMARY KEY
);
CREATE TABLE ties (
    id INTEGER PRIMARY KEY,
    stage_id INTEGER NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
    scoring_rule_set_id INTEGER NOT NULL REFERENCES scoring_rule_sets(id),
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    is_two_legged INTEGER NOT NULL DEFAULT 0 CHECK (is_two_legged IN (0, 1)),
    advancing_team_id INTEGER REFERENCES teams(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (stage_id, position)
);
CREATE INDEX idx_ties_stage_id ON ties(stage_id);
CREATE TABLE tie_audit (tie_id INTEGER NOT NULL);
CREATE TRIGGER ties_insert_audit
AFTER INSERT ON ties
BEGIN
    INSERT INTO tie_audit (tie_id) VALUES (NEW.id);
END;
CREATE TABLE matches (
    id INTEGER PRIMARY KEY,
    stage_id INTEGER NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
    tie_id INTEGER REFERENCES ties(id) ON DELETE SET NULL,
    scoring_rule_set_id INTEGER NOT NULL REFERENCES scoring_rule_sets(id),
    home_team_id INTEGER NOT NULL REFERENCES teams(id),
    away_team_id INTEGER NOT NULL REFERENCES teams(id),
    starts_at_utc TEXT NOT NULL,
    best_of INTEGER CHECK (best_of IS NULL OR best_of IN (3, 5)),
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (
        status IN ('scheduled', 'started', 'finished', 'cancelled')
    ),
    home_score_final INTEGER,
    away_score_final INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (home_team_id != away_team_id)
);
CREATE TABLE shared_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shared_tournament_id INTEGER NOT NULL
        REFERENCES shared_tournaments(id) ON DELETE CASCADE,
    home_team_id INTEGER NOT NULL REFERENCES teams(id),
    away_team_id INTEGER NOT NULL REFERENCES teams(id),
    starts_at_utc TEXT NOT NULL,
    best_of INTEGER CHECK (best_of IS NULL OR best_of IN (3, 5)),
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (
        status IN ('scheduled', 'started', 'finished', 'cancelled')
    ),
    home_score_final INTEGER,
    away_score_final INTEGER,
    advancing_team_id INTEGER REFERENCES teams(id),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (home_team_id != away_team_id)
);
CREATE TABLE shared_tournament_events (
    id INTEGER PRIMARY KEY,
    shared_tournament_id INTEGER NOT NULL
        REFERENCES shared_tournaments(id) ON DELETE CASCADE,
    shared_match_id INTEGER REFERENCES shared_matches(id) ON DELETE SET NULL,
    actor_telegram_user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    before_state TEXT,
    after_state TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _create_legacy_database(
    database_path: Path, *, best_of_added_by_migration: bool = False
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.executescript(
            """
            INSERT INTO teams (id, name) VALUES (1, 'Реал'), (2, 'Интер');
            INSERT INTO shared_tournaments (id, name) VALUES (1, 'ЛЧ');
            INSERT INTO contests (id, name) VALUES (1, 'Прогнозы');
            INSERT INTO scoring_rule_sets (id) VALUES (1);
            INSERT INTO stages (id) VALUES (1);
            INSERT INTO ties (
                id, stage_id, scoring_rule_set_id, name, position,
                advancing_team_id, created_at
            ) VALUES (10, 1, 1, 'Реал — Интер', 1, 1, '2026-01-01');
            INSERT INTO matches (
                id, stage_id, tie_id, scoring_rule_set_id,
                home_team_id, away_team_id, starts_at_utc, status,
                home_score_final, away_score_final, created_at
            ) VALUES (
                20, 1, 10, 1, 1, 2, '2026-06-01T12:00:00Z',
                'finished', 2, 1, '2026-01-01'
            );
            INSERT INTO shared_matches (
                id, shared_tournament_id, home_team_id, away_team_id,
                starts_at_utc, status, home_score_final, away_score_final,
                advancing_team_id, version, created_at, updated_at
            ) VALUES (
                30, 1, 1, 2, '2026-06-01T12:00:00Z', 'finished',
                2, 1, 1, 4, '2026-01-01', '2026-06-01'
            );
            INSERT INTO shared_tournament_events (
                id, shared_tournament_id, shared_match_id,
                actor_telegram_user_id, event_type
            ) VALUES (40, 1, 30, 123, 'shared_match.result_recorded');
            DELETE FROM tie_audit;
            """
        )
        if best_of_added_by_migration:
            # Production databases created before TI 2026 received best_of via
            # ALTER TABLE, so SQLite keeps it after created_at rather than in
            # the position used by today's CREATE TABLE statement.
            connection.executescript(
                """
                CREATE TABLE matches__historical_layout (
                    id INTEGER PRIMARY KEY,
                    stage_id INTEGER NOT NULL
                        REFERENCES stages(id) ON DELETE CASCADE,
                    tie_id INTEGER REFERENCES ties(id) ON DELETE SET NULL,
                    scoring_rule_set_id INTEGER NOT NULL
                        REFERENCES scoring_rule_sets(id),
                    home_team_id INTEGER NOT NULL REFERENCES teams(id),
                    away_team_id INTEGER NOT NULL REFERENCES teams(id),
                    starts_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (
                        status IN (
                            'scheduled', 'started', 'finished', 'cancelled'
                        )
                    ),
                    home_score_final INTEGER,
                    away_score_final INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    best_of INTEGER CHECK (
                        best_of IS NULL OR best_of IN (3, 5)
                    ),
                    CHECK (home_team_id != away_team_id)
                );
                INSERT INTO matches__historical_layout (
                    id, stage_id, tie_id, scoring_rule_set_id,
                    home_team_id, away_team_id, starts_at_utc, status,
                    home_score_final, away_score_final, created_at, best_of
                )
                SELECT
                    id, stage_id, tie_id, scoring_rule_set_id,
                    home_team_id, away_team_id, starts_at_utc, status,
                    home_score_final, away_score_final, created_at, best_of
                FROM matches;
                DROP TABLE matches;
                ALTER TABLE matches__historical_layout RENAME TO matches;
                """
            )


@pytest.mark.parametrize("best_of_added_by_migration", [False, True])
def test_migration_is_additive_preserves_rows_and_is_idempotent(
    tmp_path: Path, best_of_added_by_migration: bool
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_database(
        database_path, best_of_added_by_migration=best_of_added_by_migration
    )

    migrate_database(database_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        tie = connection.execute("SELECT * FROM ties WHERE id = 10").fetchone()
        match = connection.execute("SELECT * FROM matches WHERE id = 20").fetchone()
        shared_match = connection.execute(
            "SELECT * FROM shared_matches WHERE id = 30"
        ).fetchone()
        event = connection.execute(
            "SELECT * FROM shared_tournament_events WHERE id = 40"
        ).fetchone()
        objects = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE name IN ('idx_ties_stage_id', 'ties_insert_audit')
                """
            )
        }
        health = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert tie["advancing_team_id"] == 1
    assert tie["first_team_id"] is None
    assert tie["resolution_method"] is None
    assert match["leg_number"] is None
    assert shared_match["shared_tie_id"] is None
    assert shared_match["version"] == 4
    assert event["shared_tie_id"] is None
    assert objects == {"idx_ties_stage_id", "ties_insert_audit"}
    assert health == "ok"
    assert foreign_key_errors == []


def test_migration_refuses_partial_schema_without_changing_legacy_tables(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "partial.db"
    _create_legacy_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE shared_two_legged_ties (id INTEGER PRIMARY KEY)"
        )

    with pytest.raises(RuntimeError, match="Partially applied"):
        migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        tie_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(ties)").fetchall()
        ]
        assert connection.execute("SELECT COUNT(*) FROM ties").fetchone()[0] == 1
    assert "first_team_id" not in tie_columns


def test_migration_accepts_current_schema_as_noop(tmp_path: Path) -> None:
    database_path = tmp_path / "current.db"
    initialize_database(database_path)

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shared_two_legged_ties"
            ).fetchone()[0]
            == 0
        )


def test_migration_still_rejects_unrelated_additive_core_column(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "unexpected-current.db"
    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("ALTER TABLE shared_matches ADD COLUMN unexpected TEXT")

    with pytest.raises(RuntimeError, match="Partially applied"):
        migrate_database(database_path)


def test_migration_requires_existing_database_and_backup_is_non_destructive(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing.db"
    with pytest.raises(RuntimeError, match="does not exist"):
        migrate_database(missing_path)

    database_path = tmp_path / "legacy.db"
    backup_path = tmp_path / "backup.db"
    _create_legacy_database(database_path)
    backup_database(database_path, backup_path)
    migrate_database(database_path)

    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ties").fetchone()[0] == 1
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(ties)").fetchall()
        ]
    assert "first_team_id" not in columns
    with pytest.raises(RuntimeError, match="already exists"):
        backup_database(database_path, backup_path)
