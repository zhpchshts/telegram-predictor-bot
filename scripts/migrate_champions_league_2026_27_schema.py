from __future__ import annotations

import argparse
from pathlib import Path
import re
import sqlite3


NEW_TEMPLATE_KEY = "champions_league_2026_27"

_CONTEST_COLUMNS = (
    "id",
    "chat_id",
    "name",
    "slug",
    "template_key",
    "is_active",
    "champion_prediction_enabled",
    "champion_prediction_deadline_at",
    "champion_prediction_points",
    "champion_team_id",
    "match_prediction_publication_enabled",
    "match_prediction_publication_enabled_at",
    "created_at",
)
_CONTEST_COLUMNS_TEMPLATE_LAST = (
    "id",
    "chat_id",
    "name",
    "slug",
    "is_active",
    "champion_prediction_enabled",
    "champion_prediction_deadline_at",
    "champion_prediction_points",
    "champion_team_id",
    "match_prediction_publication_enabled",
    "match_prediction_publication_enabled_at",
    "created_at",
    "template_key",
)
_CONTEST_COLUMN_LAYOUTS = (
    _CONTEST_COLUMNS,
    _CONTEST_COLUMNS_TEMPLATE_LAST,
)
_SHARED_TOURNAMENT_COLUMNS = (
    "id",
    "name",
    "template_key",
    "is_archived",
    "version",
    "created_by_telegram_user_id",
    "created_at",
    "updated_at",
)

_OLD_TEMPLATE_CHECK = re.compile(
    r"\btemplate_key\s+IN\s*\(\s*"
    r"'world_cup_2026'\s*,\s*"
    r"'the_international_2026'\s*\)",
    re.IGNORECASE,
)
_NEW_TEMPLATE_CHECK = re.compile(
    r"\btemplate_key\s+IN\s*\(\s*"
    r"'world_cup_2026'\s*,\s*"
    r"'the_international_2026'\s*,\s*"
    r"'champions_league_2026_27'\s*\)",
    re.IGNORECASE,
)
_NEW_TEMPLATE_CHECK_SQL = (
    "template_key IN ("
    "'world_cup_2026', "
    "'the_international_2026', "
    "'champions_league_2026_27'"
    ")"
)


def migrate_database(database_path: Path) -> None:
    if not database_path.is_file():
        raise RuntimeError(f"Database file does not exist: {database_path}")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _require_healthy_database(connection, phase="before migration")

        # SQLite cannot change a CHECK constraint in place. Foreign-key
        # enforcement must be disabled before the rebuilding transaction starts;
        # foreign_key_check still verifies every relationship before commit.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            contest_columns, shared_tournament_columns = _require_supported_schema(
                connection
            )
            _expand_template_check(
                connection,
                table_name="contests",
                expected_columns=contest_columns,
            )
            _expand_template_check(
                connection,
                table_name="shared_tournaments",
                expected_columns=shared_tournament_columns,
            )
            _normalize_legacy_ucl_swiss_limits(connection)
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


def _require_supported_schema(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    contest_columns = _require_supported_columns(
        connection,
        table_name="contests",
        supported_layouts=_CONTEST_COLUMN_LAYOUTS,
    )
    shared_tournament_columns = _require_supported_columns(
        connection,
        table_name="shared_tournaments",
        supported_layouts=(_SHARED_TOURNAMENT_COLUMNS,),
    )
    _template_check_state(_table_sql(connection, "contests"), table_name="contests")
    _template_check_state(
        _table_sql(connection, "shared_tournaments"),
        table_name="shared_tournaments",
    )
    return contest_columns, shared_tournament_columns


def _expand_template_check(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    expected_columns: tuple[str, ...],
) -> None:
    original_sql = _table_sql(connection, table_name)
    if _template_check_state(original_sql, table_name=table_name) == "expanded":
        return

    expanded_sql, replacement_count = _OLD_TEMPLATE_CHECK.subn(
        _NEW_TEMPLATE_CHECK_SQL,
        original_sql,
    )
    if replacement_count != 1:
        raise RuntimeError(
            f"Could not expand {table_name}.template_key safely; "
            "refusing to rebuild the table."
        )

    opening_parenthesis = expanded_sql.find("(")
    if (
        opening_parenthesis < 0
        or re.match(
            r"\s*CREATE\s+TABLE\b",
            expanded_sql,
            re.IGNORECASE,
        )
        is None
    ):
        raise RuntimeError(
            f"Unexpected {table_name} definition; refusing to rebuild the table."
        )

    temporary_table_name = f"{table_name}__champions_league_2026_27_new"
    if _schema_object_exists(connection, temporary_table_name):
        raise RuntimeError(
            f"Temporary migration object already exists: {temporary_table_name}"
        )

    dependent_objects = _dependent_schema_objects(connection, table_name=table_name)
    column_list = ", ".join(_quote_identifier(name) for name in expected_columns)
    original_row_count = _row_count(connection, table_name)
    create_sql = (
        f"CREATE TABLE {_quote_identifier(temporary_table_name)} "
        f"{expanded_sql[opening_parenthesis:]}"
    )

    connection.execute(create_sql)
    connection.execute(
        f"INSERT INTO {_quote_identifier(temporary_table_name)} ({column_list}) "
        f"SELECT {column_list} FROM {_quote_identifier(table_name)}"
    )
    if _row_count(connection, temporary_table_name) != original_row_count:
        raise RuntimeError(
            f"Row count changed while rebuilding {table_name}; migration aborted."
        )

    connection.execute(f"DROP TABLE {_quote_identifier(table_name)}")
    connection.execute(
        f"ALTER TABLE {_quote_identifier(temporary_table_name)} "
        f"RENAME TO {_quote_identifier(table_name)}"
    )
    for object_sql in dependent_objects:
        connection.execute(object_sql)


def _template_check_state(table_sql: str, *, table_name: str) -> str:
    old_matches = _OLD_TEMPLATE_CHECK.findall(table_sql)
    new_matches = _NEW_TEMPLATE_CHECK.findall(table_sql)
    if len(old_matches) == 1 and not new_matches:
        return "historical"
    if len(new_matches) == 1 and not old_matches:
        return "expanded"
    raise RuntimeError(
        f"Unexpected {table_name}.template_key CHECK constraint; "
        "refusing to rebuild the table."
    )


def _require_expected_schema(connection: sqlite3.Connection) -> None:
    _require_supported_schema(connection)
    for table_name in ("contests", "shared_tournaments"):
        state = _template_check_state(
            _table_sql(connection, table_name),
            table_name=table_name,
        )
        if state != "expanded":
            raise RuntimeError(
                f"Migration did not expand {table_name}.template_key CHECK constraint."
            )


def _normalize_legacy_ucl_swiss_limits(connection: sqlite3.Connection) -> None:
    _require_consistent_ucl_link_templates(connection)
    _normalize_legacy_ucl_contest_limits(connection)
    _normalize_legacy_ucl_shared_limits(connection)
    _require_consistent_ucl_linked_limits(connection)


def _require_consistent_ucl_link_templates(connection: sqlite3.Connection) -> None:
    if not _schema_object_exists(connection, "contest_shared_tournaments"):
        return
    mismatch = connection.execute(
        """
        SELECT links.contest_id, links.shared_tournament_id
        FROM contest_shared_tournaments AS links
        JOIN contests ON contests.id = links.contest_id
        JOIN shared_tournaments AS tournaments
            ON tournaments.id = links.shared_tournament_id
        WHERE (contests.template_key = ? OR tournaments.template_key = ?)
          AND contests.template_key != tournaments.template_key
        LIMIT 1
        """,
        (NEW_TEMPLATE_KEY, NEW_TEMPLATE_KEY),
    ).fetchone()
    if mismatch is not None:
        raise RuntimeError(
            "Champions League contest/shared tournament template mismatch for "
            f"contest {int(mismatch[0])} and tournament {int(mismatch[1])}."
        )


def _normalize_legacy_ucl_contest_limits(connection: sqlite3.Connection) -> None:
    contest_count = _ucl_entity_count(connection, table_name="contests")
    if contest_count == 0:
        return
    required_tables = {
        "contest_shared_tournaments",
        "swiss_stage_prediction_settings",
        "swiss_stage_predictions",
        "swiss_stage_results",
        "contest_teams",
    }
    _require_schema_objects(
        connection,
        object_names=required_tables,
        context="Champions League contests",
    )
    rows = connection.execute(
        """
        SELECT
            contests.id,
            settings.contest_id,
            settings.enabled,
            settings.direct_qualifier_count,
            settings.elimination_qualifier_count,
            (SELECT COUNT(*) FROM contest_teams
             WHERE contest_teams.contest_id = contests.id) AS team_count,
            EXISTS (
                SELECT 1 FROM swiss_stage_predictions
                WHERE swiss_stage_predictions.contest_id = contests.id
            ) AS has_predictions,
            EXISTS (
                SELECT 1 FROM swiss_stage_results
                WHERE swiss_stage_results.contest_id = contests.id
            ) AS has_result
        FROM contests
        LEFT JOIN swiss_stage_prediction_settings AS settings
            ON settings.contest_id = contests.id
        WHERE contests.template_key = ?
        ORDER BY contests.id
        """,
        (NEW_TEMPLATE_KEY,),
    ).fetchall()
    for row in rows:
        contest_id = int(row[0])
        if row[1] is None:
            raise RuntimeError(
                f"Champions League contest {contest_id} is missing Swiss settings."
            )
        enabled = bool(row[2])
        direct_count = int(row[3])
        elimination_count = int(row[4])
        team_count = int(row[5])
        has_predictions = bool(row[6])
        has_result = bool(row[7])
        _require_supported_ucl_limits(
            entity_label=f"contest {contest_id}",
            direct_count=direct_count,
            elimination_count=elimination_count,
        )
        if enabled and team_count != 36:
            raise RuntimeError(
                f"Enabled Champions League contest {contest_id} has {team_count} "
                "teams instead of 36."
            )
        if (direct_count, elimination_count) == (8, 12):
            continue
        if has_predictions or has_result:
            raise RuntimeError(
                f"Champions League contest {contest_id} still uses legacy 8+16 "
                "limits and already has predictions or a result; resolve it "
                "manually before migration."
            )
        connection.execute(
            """
            UPDATE swiss_stage_prediction_settings
            SET elimination_qualifier_count = 12,
                updated_at = CURRENT_TIMESTAMP
            WHERE contest_id = ?
            """,
            (contest_id,),
        )


def _normalize_legacy_ucl_shared_limits(connection: sqlite3.Connection) -> None:
    tournament_count = _ucl_entity_count(connection, table_name="shared_tournaments")
    if tournament_count == 0:
        return
    required_tables = {
        "contest_shared_tournaments",
        "shared_tournament_settings",
        "shared_tournament_teams",
        "shared_swiss_stage_result_selections",
        "swiss_stage_prediction_settings",
        "swiss_stage_predictions",
        "swiss_stage_results",
    }
    _require_schema_objects(
        connection,
        object_names=required_tables,
        context="Champions League shared tournaments",
    )
    rows = connection.execute(
        """
        SELECT
            tournaments.id,
            settings.shared_tournament_id,
            settings.swiss_stage_prediction_enabled,
            settings.swiss_direct_qualifier_count,
            settings.swiss_elimination_qualifier_count,
            (SELECT COUNT(*) FROM shared_tournament_teams
             WHERE shared_tournament_teams.shared_tournament_id =
                   tournaments.id) AS team_count,
            EXISTS (
                SELECT 1 FROM shared_swiss_stage_result_selections AS results
                WHERE results.shared_tournament_id = tournaments.id
            ) AS has_result
        FROM shared_tournaments AS tournaments
        LEFT JOIN shared_tournament_settings AS settings
            ON settings.shared_tournament_id = tournaments.id
        WHERE tournaments.template_key = ?
        ORDER BY tournaments.id
        """,
        (NEW_TEMPLATE_KEY,),
    ).fetchall()
    for row in rows:
        tournament_id = int(row[0])
        if row[1] is None:
            raise RuntimeError(
                "Champions League shared tournament "
                f"{tournament_id} is missing Swiss settings."
            )
        enabled = bool(row[2])
        direct_count = int(row[3])
        elimination_count = int(row[4])
        team_count = int(row[5])
        has_result = bool(row[6])
        _require_supported_ucl_limits(
            entity_label=f"shared tournament {tournament_id}",
            direct_count=direct_count,
            elimination_count=elimination_count,
        )
        if enabled and team_count != 36:
            raise RuntimeError(
                f"Enabled Champions League shared tournament {tournament_id} "
                f"has {team_count} teams instead of 36."
            )
        if (direct_count, elimination_count) == (8, 12):
            continue
        has_linked_predictions = _shared_tournament_has_linked_swiss_data(
            connection,
            shared_tournament_id=tournament_id,
        )
        if has_result or has_linked_predictions:
            raise RuntimeError(
                f"Champions League shared tournament {tournament_id} still uses "
                "legacy 8+16 limits and already has predictions or a result; "
                "resolve it manually before migration."
            )
        connection.execute(
            """
            UPDATE shared_tournament_settings
            SET swiss_elimination_qualifier_count = 12,
                updated_at = CURRENT_TIMESTAMP
            WHERE shared_tournament_id = ?
            """,
            (tournament_id,),
        )


def _shared_tournament_has_linked_swiss_data(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM contest_shared_tournaments AS links
            WHERE links.shared_tournament_id = ?
              AND (
                EXISTS (
                    SELECT 1 FROM swiss_stage_predictions AS predictions
                    WHERE predictions.contest_id = links.contest_id
                )
                OR EXISTS (
                    SELECT 1 FROM swiss_stage_results AS results
                    WHERE results.contest_id = links.contest_id
                )
              )
            LIMIT 1
            """,
            (shared_tournament_id,),
        ).fetchone()
        is not None
    )


def _require_supported_ucl_limits(
    *,
    entity_label: str,
    direct_count: int,
    elimination_count: int,
) -> None:
    if (direct_count, elimination_count) not in {(8, 12), (8, 16)}:
        raise RuntimeError(
            f"Champions League {entity_label} has unsupported limits "
            f"{direct_count}+{elimination_count}; expected 8+12 or legacy 8+16."
        )


def _ucl_entity_count(
    connection: sqlite3.Connection,
    *,
    table_name: str,
) -> int:
    if table_name not in {"contests", "shared_tournaments"}:
        raise RuntimeError(f"Unsupported UCL entity table: {table_name}")
    row = connection.execute(
        f"SELECT COUNT(*) FROM {_quote_identifier(table_name)} WHERE template_key = ?",
        (NEW_TEMPLATE_KEY,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _require_schema_objects(
    connection: sqlite3.Connection,
    *,
    object_names: set[str],
    context: str,
) -> None:
    missing = sorted(
        name for name in object_names if not _schema_object_exists(connection, name)
    )
    if missing:
        raise RuntimeError(
            f"Incomplete schema for {context}; missing: {', '.join(missing)}."
        )


def _require_consistent_ucl_linked_limits(connection: sqlite3.Connection) -> None:
    if (
        _ucl_entity_count(connection, table_name="contests") == 0
        and _ucl_entity_count(connection, table_name="shared_tournaments") == 0
    ):
        return
    required_tables = {
        "contest_shared_tournaments",
        "swiss_stage_prediction_settings",
        "shared_tournament_settings",
    }
    _require_schema_objects(
        connection,
        object_names=required_tables,
        context="Champions League linked settings",
    )
    mismatch = connection.execute(
        """
        SELECT links.contest_id, links.shared_tournament_id
        FROM contest_shared_tournaments AS links
        JOIN contests ON contests.id = links.contest_id
        JOIN shared_tournaments AS tournaments
            ON tournaments.id = links.shared_tournament_id
        LEFT JOIN swiss_stage_prediction_settings AS contest_settings
            ON contest_settings.contest_id = links.contest_id
        LEFT JOIN shared_tournament_settings AS shared_settings
            ON shared_settings.shared_tournament_id = links.shared_tournament_id
        WHERE contests.template_key = ?
          AND (
            contest_settings.contest_id IS NULL
            OR shared_settings.shared_tournament_id IS NULL
            OR contest_settings.direct_qualifier_count !=
               shared_settings.swiss_direct_qualifier_count
            OR contest_settings.elimination_qualifier_count !=
               shared_settings.swiss_elimination_qualifier_count
          )
        LIMIT 1
        """,
        (NEW_TEMPLATE_KEY,),
    ).fetchone()
    if mismatch is not None:
        raise RuntimeError(
            "Champions League linked Swiss settings are inconsistent for contest "
            f"{int(mismatch[0])} and tournament {int(mismatch[1])}."
        )


def _require_supported_columns(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    supported_layouts: tuple[tuple[str, ...], ...],
) -> tuple[str, ...]:
    actual_columns = _column_names(connection, table_name)
    if actual_columns not in supported_layouts:
        raise RuntimeError(
            f"Unexpected {table_name} schema; refusing to rebuild the table. "
            f"Expected one of {supported_layouts!r}, found {actual_columns!r}."
        )
    return actual_columns


def _column_names(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_quote_identifier(table_name)})"
        ).fetchall()
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


def _dependent_schema_objects(
    connection: sqlite3.Connection,
    *,
    table_name: str,
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


def _schema_object_exists(connection: sqlite3.Connection, object_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?",
            (object_name,),
        ).fetchone()
        is not None
    )


def _row_count(connection: sqlite3.Connection, table_name: str) -> int:
    row = connection.execute(
        f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}"
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Could not count rows in {table_name}.")
    return int(row[0])


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


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
        description=(
            "Prepare Klever schema and safe legacy settings for Champions "
            "League 2026/27."
        )
    )
    parser.add_argument("database_path", type=Path)
    arguments = parser.parse_args()

    migrate_database(arguments.database_path)
    print(f"Migration completed: {arguments.database_path}")


if __name__ == "__main__":
    main()
