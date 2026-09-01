from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


_LEGACY_TIE_COLUMNS = (
    "id",
    "stage_id",
    "scoring_rule_set_id",
    "name",
    "position",
    "is_two_legged",
    "advancing_team_id",
    "created_at",
)
_CURRENT_TIE_COLUMNS = (
    "id",
    "stage_id",
    "scoring_rule_set_id",
    "name",
    "position",
    "is_two_legged",
    "first_team_id",
    "second_team_id",
    "advancing_team_id",
    "resolution_method",
    "second_leg_extra_time_home_score",
    "second_leg_extra_time_away_score",
    "second_leg_home_penalty_score",
    "second_leg_away_penalty_score",
    "created_at",
)
_LEGACY_MATCH_COLUMNS = (
    "id",
    "stage_id",
    "tie_id",
    "scoring_rule_set_id",
    "home_team_id",
    "away_team_id",
    "starts_at_utc",
    "best_of",
    "status",
    "home_score_final",
    "away_score_final",
    "created_at",
)
_LEGACY_MATCH_COLUMNS_BEST_OF_LAST = (
    "id",
    "stage_id",
    "tie_id",
    "scoring_rule_set_id",
    "home_team_id",
    "away_team_id",
    "starts_at_utc",
    "status",
    "home_score_final",
    "away_score_final",
    "created_at",
    "best_of",
)
_CURRENT_MATCH_COLUMNS = (
    _LEGACY_MATCH_COLUMNS[:7] + ("leg_number",) + _LEGACY_MATCH_COLUMNS[7:]
)
_MIGRATED_MATCH_COLUMNS = _LEGACY_MATCH_COLUMNS + ("leg_number",)
_MIGRATED_MATCH_COLUMNS_BEST_OF_LAST = _LEGACY_MATCH_COLUMNS_BEST_OF_LAST + (
    "leg_number",
)
_LEGACY_SHARED_MATCH_COLUMNS = (
    "id",
    "shared_tournament_id",
    "home_team_id",
    "away_team_id",
    "starts_at_utc",
    "best_of",
    "status",
    "home_score_final",
    "away_score_final",
    "advancing_team_id",
    "version",
    "created_at",
    "updated_at",
)
_CURRENT_SHARED_MATCH_COLUMNS = (
    _LEGACY_SHARED_MATCH_COLUMNS[:2]
    + (
        "shared_tie_id",
        "leg_number",
    )
    + _LEGACY_SHARED_MATCH_COLUMNS[2:]
)
_MIGRATED_SHARED_MATCH_COLUMNS = _LEGACY_SHARED_MATCH_COLUMNS + (
    "shared_tie_id",
    "leg_number",
)
_LEGACY_SHARED_EVENT_COLUMNS = (
    "id",
    "shared_tournament_id",
    "shared_match_id",
    "actor_telegram_user_id",
    "event_type",
    "before_state",
    "after_state",
    "metadata",
    "created_at",
)
_CURRENT_SHARED_EVENT_COLUMNS = (
    _LEGACY_SHARED_EVENT_COLUMNS[:3]
    + ("shared_tie_id",)
    + _LEGACY_SHARED_EVENT_COLUMNS[3:]
)
_MIGRATED_SHARED_EVENT_COLUMNS = _LEGACY_SHARED_EVENT_COLUMNS + ("shared_tie_id",)

_CURRENT_SHARED_TIE_COLUMNS = (
    "id",
    "shared_tournament_id",
    "first_team_id",
    "second_team_id",
    "advancing_team_id",
    "resolution_method",
    "second_leg_extra_time_home_score",
    "second_leg_extra_time_away_score",
    "second_leg_home_penalty_score",
    "second_leg_away_penalty_score",
    "version",
    "created_at",
    "updated_at",
)
_BRACKET_SHARED_TIE_ADDITIONS = frozenset({"round_key", "bracket_position"})
_BRACKET_SHARED_MATCH_ADDITIONS = frozenset({"round_key", "bracket_position"})

_NEW_TABLES = ("shared_two_legged_ties", "shared_tie_links")


def migrate_database(database_path: Path) -> None:
    if not database_path.is_file():
        raise RuntimeError(f"Database file does not exist: {database_path}")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _require_healthy_database(connection, phase="before migration")
        state = _schema_state(connection)
        if state == "current":
            _require_expected_schema(connection)
            return
        if state != "legacy":
            raise RuntimeError(
                "Partially applied or unexpected two-legged tie schema; "
                "refusing to migrate it."
            )

        # The ties table must be rebuilt to add cross-column CHECK constraints.
        # SQLite only allows foreign-key enforcement to be toggled before the
        # rebuilding transaction begins. foreign_key_check is still run before
        # the transaction commits.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            _create_shared_two_legged_ties(connection)
            _rebuild_ties(connection)
            _add_column(
                connection,
                table_name="matches",
                definition=(
                    "leg_number INTEGER "
                    "CHECK (leg_number IS NULL OR leg_number IN (1, 2))"
                ),
            )
            _add_column(
                connection,
                table_name="shared_matches",
                definition=(
                    "shared_tie_id INTEGER REFERENCES "
                    "shared_two_legged_ties(id) ON DELETE CASCADE"
                ),
            )
            _add_column(
                connection,
                table_name="shared_matches",
                definition=(
                    "leg_number INTEGER "
                    "CHECK (leg_number IS NULL OR leg_number IN (1, 2))"
                ),
            )
            _add_column(
                connection,
                table_name="shared_tournament_events",
                definition=(
                    "shared_tie_id INTEGER REFERENCES "
                    "shared_two_legged_ties(id) ON DELETE SET NULL"
                ),
            )
            _create_indexes_and_links(connection)
            _require_expected_schema(connection)
            _require_healthy_database(connection, phase="during migration")
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


def backup_database(source_path: Path, backup_path: Path) -> None:
    if not source_path.is_file():
        raise RuntimeError(f"Database file does not exist: {source_path}")
    if backup_path.exists():
        raise RuntimeError(f"Backup path already exists: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)


def _schema_state(connection: sqlite3.Connection) -> str:
    required_tables = {
        "teams",
        "shared_tournaments",
        "shared_matches",
        "contests",
        "ties",
        "matches",
        "shared_tournament_events",
    }
    missing_tables = sorted(
        table_name
        for table_name in required_tables
        if not _schema_object_exists(connection, table_name, object_type="table")
    )
    if missing_tables:
        raise RuntimeError("Required tables are missing: " + ", ".join(missing_tables))

    tie_columns = _column_names(connection, "ties")
    match_columns = _column_names(connection, "matches")
    shared_match_columns = _column_names(connection, "shared_matches")
    shared_event_columns = _column_names(connection, "shared_tournament_events")
    new_table_count = sum(
        _schema_object_exists(connection, table_name, object_type="table")
        for table_name in _NEW_TABLES
    )

    if (
        tie_columns == _LEGACY_TIE_COLUMNS
        and match_columns in {_LEGACY_MATCH_COLUMNS, _LEGACY_MATCH_COLUMNS_BEST_OF_LAST}
        and shared_match_columns == _LEGACY_SHARED_MATCH_COLUMNS
        and shared_event_columns == _LEGACY_SHARED_EVENT_COLUMNS
        and new_table_count == 0
    ):
        return "legacy"

    if (
        tie_columns == _CURRENT_TIE_COLUMNS
        and match_columns
        in {
            _CURRENT_MATCH_COLUMNS,
            _MIGRATED_MATCH_COLUMNS,
            _MIGRATED_MATCH_COLUMNS_BEST_OF_LAST,
        }
        and _columns_match_with_allowed_additions(
            shared_match_columns,
            expected_variants=(
                _CURRENT_SHARED_MATCH_COLUMNS,
                _MIGRATED_SHARED_MATCH_COLUMNS,
            ),
            allowed_additions=_BRACKET_SHARED_MATCH_ADDITIONS,
        )
        and shared_event_columns
        in {_CURRENT_SHARED_EVENT_COLUMNS, _MIGRATED_SHARED_EVENT_COLUMNS}
        and new_table_count == len(_NEW_TABLES)
        and _columns_match_with_allowed_additions(
            _column_names(connection, "shared_two_legged_ties"),
            expected_variants=(_CURRENT_SHARED_TIE_COLUMNS,),
            allowed_additions=_BRACKET_SHARED_TIE_ADDITIONS,
        )
    ):
        return "current"
    return "unexpected"


def _columns_match_with_allowed_additions(
    actual: tuple[str, ...],
    *,
    expected_variants: tuple[tuple[str, ...], ...],
    allowed_additions: frozenset[str],
) -> bool:
    actual_additions = set(actual).intersection(allowed_additions)
    unexpected = set(actual).difference(
        *(set(expected) for expected in expected_variants),
        allowed_additions,
    )
    if unexpected or not actual_additions.issubset(allowed_additions):
        return False
    filtered = tuple(column for column in actual if column not in allowed_additions)
    return filtered in expected_variants


def _create_shared_two_legged_ties(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE shared_two_legged_ties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shared_tournament_id INTEGER NOT NULL
                REFERENCES shared_tournaments(id) ON DELETE CASCADE,
            first_team_id INTEGER NOT NULL REFERENCES teams(id),
            second_team_id INTEGER NOT NULL REFERENCES teams(id),
            advancing_team_id INTEGER REFERENCES teams(id),
            resolution_method TEXT CHECK (
                resolution_method IS NULL
                OR resolution_method IN ('aggregate', 'extra_time', 'penalties')
            ),
            second_leg_extra_time_home_score INTEGER CHECK (
                second_leg_extra_time_home_score IS NULL
                OR second_leg_extra_time_home_score >= 0
            ),
            second_leg_extra_time_away_score INTEGER CHECK (
                second_leg_extra_time_away_score IS NULL
                OR second_leg_extra_time_away_score >= 0
            ),
            second_leg_home_penalty_score INTEGER CHECK (
                second_leg_home_penalty_score IS NULL
                OR second_leg_home_penalty_score >= 0
            ),
            second_leg_away_penalty_score INTEGER CHECK (
                second_leg_away_penalty_score IS NULL
                OR second_leg_away_penalty_score >= 0
            ),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (first_team_id != second_team_id),
            CHECK (
                advancing_team_id IS NULL
                OR advancing_team_id = first_team_id
                OR advancing_team_id = second_team_id
            ),
            CHECK (
                (second_leg_extra_time_home_score IS NULL) =
                (second_leg_extra_time_away_score IS NULL)
            ),
            CHECK (
                (second_leg_home_penalty_score IS NULL) =
                (second_leg_away_penalty_score IS NULL)
            )
        )
        """
    )


def _rebuild_ties(connection: sqlite3.Connection) -> None:
    temporary_table = "ties__two_legged_new"
    if _schema_object_exists(connection, temporary_table, object_type="table"):
        raise RuntimeError(
            f"Temporary migration table already exists: {temporary_table}"
        )
    dependent_objects = _dependent_schema_objects(connection, table_name="ties")
    before_count = _row_count(connection, "ties")
    connection.execute(
        f"""
        CREATE TABLE {temporary_table} (
            id INTEGER PRIMARY KEY,
            stage_id INTEGER NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
            scoring_rule_set_id INTEGER NOT NULL REFERENCES scoring_rule_sets(id),
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            is_two_legged INTEGER NOT NULL DEFAULT 0
                CHECK (is_two_legged IN (0, 1)),
            first_team_id INTEGER REFERENCES teams(id),
            second_team_id INTEGER REFERENCES teams(id),
            advancing_team_id INTEGER REFERENCES teams(id),
            resolution_method TEXT CHECK (
                resolution_method IS NULL
                OR resolution_method IN ('aggregate', 'extra_time', 'penalties')
            ),
            second_leg_extra_time_home_score INTEGER CHECK (
                second_leg_extra_time_home_score IS NULL
                OR second_leg_extra_time_home_score >= 0
            ),
            second_leg_extra_time_away_score INTEGER CHECK (
                second_leg_extra_time_away_score IS NULL
                OR second_leg_extra_time_away_score >= 0
            ),
            second_leg_home_penalty_score INTEGER CHECK (
                second_leg_home_penalty_score IS NULL
                OR second_leg_home_penalty_score >= 0
            ),
            second_leg_away_penalty_score INTEGER CHECK (
                second_leg_away_penalty_score IS NULL
                OR second_leg_away_penalty_score >= 0
            ),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (stage_id, position),
            CHECK (
                (first_team_id IS NULL AND second_team_id IS NULL)
                OR (
                    first_team_id IS NOT NULL
                    AND second_team_id IS NOT NULL
                    AND first_team_id != second_team_id
                )
            ),
            CHECK (
                first_team_id IS NULL
                OR advancing_team_id IS NULL
                OR advancing_team_id = first_team_id
                OR advancing_team_id = second_team_id
            ),
            CHECK (
                (second_leg_extra_time_home_score IS NULL) =
                (second_leg_extra_time_away_score IS NULL)
            ),
            CHECK (
                (second_leg_home_penalty_score IS NULL) =
                (second_leg_away_penalty_score IS NULL)
            )
        )
        """
    )
    connection.execute(
        f"""
        INSERT INTO {temporary_table} (
            id, stage_id, scoring_rule_set_id, name, position,
            is_two_legged, advancing_team_id, created_at
        )
        SELECT id, stage_id, scoring_rule_set_id, name, position,
               is_two_legged, advancing_team_id, created_at
        FROM ties
        """
    )
    if _row_count(connection, temporary_table) != before_count:
        raise RuntimeError("Tie row count changed while rebuilding the table.")
    connection.execute("DROP TABLE ties")
    connection.execute(f"ALTER TABLE {temporary_table} RENAME TO ties")
    for object_sql in dependent_objects:
        connection.execute(object_sql)


def _add_column(
    connection: sqlite3.Connection, *, table_name: str, definition: str
) -> None:
    column_name = definition.split(maxsplit=1)[0]
    if column_name in _column_names(connection, table_name):
        raise RuntimeError(
            f"Unexpected pre-existing column {table_name}.{column_name}."
        )
    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


def _create_indexes_and_links(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE INDEX idx_shared_two_legged_ties_tournament
            ON shared_two_legged_ties(shared_tournament_id, id)
        """,
        """
        CREATE UNIQUE INDEX idx_shared_matches_tie_leg_number
            ON shared_matches(shared_tie_id, leg_number)
            WHERE shared_tie_id IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_matches_tie_leg_number
            ON matches(tie_id, leg_number)
            WHERE leg_number IS NOT NULL
        """,
        """
        CREATE TABLE shared_tie_links (
            shared_tie_id INTEGER NOT NULL
                REFERENCES shared_two_legged_ties(id) ON DELETE CASCADE,
            tie_id INTEGER NOT NULL REFERENCES ties(id) ON DELETE CASCADE,
            contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
            linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (shared_tie_id, contest_id),
            UNIQUE (tie_id)
        )
        """,
        """
        CREATE INDEX idx_shared_tie_links_contest
            ON shared_tie_links(contest_id, tie_id)
        """,
        """
        CREATE INDEX idx_shared_tournament_events_tie
            ON shared_tournament_events(shared_tie_id, id)
            WHERE shared_tie_id IS NOT NULL
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _require_expected_schema(connection: sqlite3.Connection) -> None:
    if _schema_state(connection) != "current":
        raise RuntimeError("Two-legged tie migration did not create expected schema.")
    expected_indexes = (
        "idx_shared_two_legged_ties_tournament",
        "idx_shared_matches_tie_leg_number",
        "idx_matches_tie_leg_number",
        "idx_shared_tie_links_contest",
        "idx_shared_tournament_events_tie",
    )
    missing_indexes = [
        name
        for name in expected_indexes
        if not _schema_object_exists(connection, name, object_type="index")
    ]
    if missing_indexes:
        raise RuntimeError(
            "Migration indexes are missing: " + ", ".join(missing_indexes)
        )


def _column_names(connection: sqlite3.Connection, table_name: str) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    )


def _schema_object_exists(
    connection: sqlite3.Connection, object_name: str, *, object_type: str
) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
            (object_type, object_name),
        ).fetchone()
        is not None
    )


def _dependent_schema_objects(
    connection: sqlite3.Connection, *, table_name: str
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE tbl_name = ?
          AND type IN ('index', 'trigger')
          AND sql IS NOT NULL
        ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END, name
        """,
        (table_name,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _row_count(connection: sqlite3.Connection, table_name: str) -> int:
    row = connection.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
    if row is None:
        raise RuntimeError(f"Could not count rows in {table_name}.")
    return int(row[0])


def _require_healthy_database(connection: sqlite3.Connection, *, phase: str) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise RuntimeError(f"Database integrity check failed {phase}.")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"Foreign key check failed {phase}: {foreign_key_errors!r}")


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add production schema support for two-legged ties."
    )
    parser.add_argument("database_path", type=Path)
    parser.add_argument("--backup-path", type=Path)
    return parser.parse_args()


def main() -> None:
    arguments = _parse_arguments()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = arguments.backup_path or arguments.database_path.with_name(
        f"{arguments.database_path.name}.{timestamp}.bak"
    )
    backup_database(arguments.database_path, backup_path)
    migrate_database(arguments.database_path)
    print(f"Backup: {backup_path}")
    print(f"Migration completed: {arguments.database_path}")


if __name__ == "__main__":
    main()
