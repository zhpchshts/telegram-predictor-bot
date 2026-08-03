from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3


def migrate_database(database_path: Path) -> None:
    if not database_path.is_file():
        raise RuntimeError(f"Database file does not exist: {database_path}")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _require_healthy_database(connection, phase="before migration")

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            _add_contest_template_key(connection)
            _add_match_best_of(connection)
            _expand_competition_types(connection)
            _create_champion_candidate_table(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

        _require_expected_schema(connection)
        _require_healthy_database(connection, phase="after migration")
    finally:
        connection.close()


def _add_contest_template_key(connection: sqlite3.Connection) -> None:
    if "template_key" in _column_names(connection, "contests"):
        return

    connection.execute(
        """
        ALTER TABLE contests
        ADD COLUMN template_key TEXT NOT NULL DEFAULT 'world_cup_2026'
        CHECK (template_key IN ('world_cup_2026', 'the_international_2026'))
        """
    )


def _add_match_best_of(connection: sqlite3.Connection) -> None:
    if "best_of" in _column_names(connection, "matches"):
        return

    connection.execute(
        """
        ALTER TABLE matches
        ADD COLUMN best_of INTEGER CHECK (best_of IS NULL OR best_of IN (3, 5))
        """
    )


def _expand_competition_types(connection: sqlite3.Connection) -> None:
    table_sql = _table_sql(connection, "competitions")
    if "the_international" in table_sql:
        return

    expected_columns = {
        "id",
        "contest_id",
        "name",
        "season",
        "competition_type",
        "is_active",
        "created_at",
    }
    actual_columns = set(_column_names(connection, "competitions"))
    if actual_columns != expected_columns:
        raise RuntimeError(
            "Unexpected competitions schema; refusing to rebuild the table."
        )

    connection.execute(
        """
        CREATE TABLE competitions_new (
            id INTEGER PRIMARY KEY,
            contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            season TEXT NOT NULL,
            competition_type TEXT NOT NULL CHECK (
                competition_type IN (
                    'world_cup',
                    'champions_league',
                    'europa_league',
                    'the_international',
                    'other'
                )
            ),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (contest_id, name, season)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO competitions_new (
            id,
            contest_id,
            name,
            season,
            competition_type,
            is_active,
            created_at
        )
        SELECT
            id,
            contest_id,
            name,
            season,
            competition_type,
            is_active,
            created_at
        FROM competitions
        """
    )
    connection.execute("DROP TABLE competitions")
    connection.execute("ALTER TABLE competitions_new RENAME TO competitions")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_competitions_contest_id
        ON competitions(contest_id)
        """
    )


def _create_champion_candidate_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS champion_prediction_candidates (
            contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
            team_id INTEGER NOT NULL REFERENCES teams(id),
            position INTEGER NOT NULL CHECK (position >= 0),
            PRIMARY KEY (contest_id, team_id),
            UNIQUE (contest_id, position)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_champion_prediction_candidates_team_id
        ON champion_prediction_candidates(team_id)
        """
    )


def _column_names(connection: sqlite3.Connection, table_name: str) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    )


def _table_sql(connection: sqlite3.Connection, table_name: str) -> str:
    row = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(f"Required table is missing: {table_name}")
    return str(row[0])


def _require_expected_schema(connection: sqlite3.Connection) -> None:
    if "template_key" not in _column_names(connection, "contests"):
        raise RuntimeError("Migration did not add contests.template_key.")
    if "best_of" not in _column_names(connection, "matches"):
        raise RuntimeError("Migration did not add matches.best_of.")
    if "the_international" not in _table_sql(connection, "competitions"):
        raise RuntimeError("Migration did not expand competitions.competition_type.")
    _table_sql(connection, "champion_prediction_candidates")


def _require_healthy_database(
    connection: sqlite3.Connection,
    *,
    phase: str,
) -> None:
    integrity_result = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity_result is None or integrity_result[0] != "ok":
        raise RuntimeError(f"Database integrity check failed {phase}.")

    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"Foreign key check failed {phase}: {foreign_key_errors!r}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate an existing Klever database for The International 2026."
    )
    parser.add_argument("database_path", type=Path)
    arguments = parser.parse_args()

    migrate_database(arguments.database_path)
    print(f"Migration completed: {arguments.database_path}")


if __name__ == "__main__":
    main()
