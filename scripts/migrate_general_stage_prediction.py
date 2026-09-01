from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


CHAMPIONS_LEAGUE_TEMPLATE_KEY = "champions_league_2026_27"
CHAMPIONS_LEAGUE_DIRECT_COUNT = 8
CHAMPIONS_LEAGUE_ELIMINATION_COUNT = 12

_LOCAL_POLICY_COLUMNS = {
    "selection_mode": (
        "TEXT NOT NULL DEFAULT 'exact' "
        "CHECK (selection_mode IN ('exact', 'up_to_limits'))"
    ),
    "direct_correct_points": (
        "INTEGER NOT NULL DEFAULT 2 CHECK (direct_correct_points >= 0)"
    ),
    "elimination_correct_points": (
        "INTEGER NOT NULL DEFAULT 2 CHECK (elimination_correct_points >= 0)"
    ),
    "cross_category_points": (
        "INTEGER NOT NULL DEFAULT 1 CHECK (cross_category_points >= 0)"
    ),
}
_SHARED_POLICY_COLUMNS = {
    "swiss_selection_mode": (
        "TEXT NOT NULL DEFAULT 'exact' "
        "CHECK (swiss_selection_mode IN ('exact', 'up_to_limits'))"
    ),
    "swiss_direct_correct_points": (
        "INTEGER NOT NULL DEFAULT 2 CHECK (swiss_direct_correct_points >= 0)"
    ),
    "swiss_elimination_correct_points": (
        "INTEGER NOT NULL DEFAULT 2 CHECK (swiss_elimination_correct_points >= 0)"
    ),
    "swiss_cross_category_points": (
        "INTEGER NOT NULL DEFAULT 1 CHECK (swiss_cross_category_points >= 0)"
    ),
}
_REQUIRED_TABLES = (
    "contests",
    "contest_teams",
    "swiss_stage_prediction_settings",
    "swiss_stage_predictions",
    "swiss_stage_prediction_selections",
    "swiss_stage_results",
    "swiss_stage_result_selections",
    "shared_tournaments",
    "shared_tournament_settings",
    "shared_tournament_teams",
    "shared_swiss_stage_result_selections",
    "contest_shared_tournaments",
)


@dataclass(frozen=True, slots=True)
class MigrationReport:
    champions_league_contest_count: int
    champions_league_shared_tournament_count: int
    prediction_count: int
    local_result_count: int
    shared_result_count: int
    normalized_local_legacy_limit_count: int
    normalized_shared_legacy_limit_count: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def migrate_database(
    database_path: Path, *, now_utc: datetime | None = None
) -> MigrationReport:
    if not database_path.is_file():
        raise RuntimeError(f"Database file does not exist: {database_path}")

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        resolved_now = _resolve_now(now_utc)
        connection.execute("PRAGMA foreign_keys = ON")
        _require_healthy_database(connection, phase="before migration")
        _require_tables(connection)
        local_state = _policy_schema_state(
            connection,
            table_name="swiss_stage_prediction_settings",
            expected_columns=tuple(_LOCAL_POLICY_COLUMNS),
        )
        shared_state = _policy_schema_state(
            connection,
            table_name="shared_tournament_settings",
            expected_columns=tuple(_SHARED_POLICY_COLUMNS),
        )

        connection.execute("BEGIN IMMEDIATE")
        try:
            if local_state == "legacy":
                _add_policy_columns(
                    connection,
                    table_name="swiss_stage_prediction_settings",
                    definitions=_LOCAL_POLICY_COLUMNS,
                )
            if shared_state == "legacy":
                _add_policy_columns(
                    connection,
                    table_name="shared_tournament_settings",
                    definitions=_SHARED_POLICY_COLUMNS,
                )

            normalized_local_count = _normalize_unlocked_legacy_local_limits(connection)
            normalized_shared_count = _normalize_unlocked_legacy_shared_limits(
                connection
            )
            _backfill_champions_league_policy(connection)
            report = _audit_general_stage_data(
                connection,
                normalized_local_count=normalized_local_count,
                normalized_shared_count=normalized_shared_count,
                now_utc=resolved_now,
            )
            _require_expected_schema(connection)
            _require_healthy_database(connection, phase="during migration")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        _require_expected_schema(connection)
        _require_healthy_database(connection, phase="after migration")
        return report
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


def _require_tables(connection: sqlite3.Connection) -> None:
    missing = [
        table_name
        for table_name in _REQUIRED_TABLES
        if not _schema_object_exists(connection, table_name, object_type="table")
    ]
    if missing:
        raise RuntimeError("Required tables are missing: " + ", ".join(missing))


def _policy_schema_state(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    expected_columns: tuple[str, ...],
) -> str:
    columns = set(_column_names(connection, table_name))
    present = [column for column in expected_columns if column in columns]
    if not present:
        return "legacy"
    if len(present) == len(expected_columns):
        return "current"
    raise RuntimeError(
        f"Partially applied general-stage schema in {table_name}; "
        "refusing to migrate it."
    )


def _add_policy_columns(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    definitions: dict[str, str],
) -> None:
    for column_name, definition in definitions.items():
        connection.execute(
            f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}'
        )


def _normalize_unlocked_legacy_local_limits(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        """
        SELECT settings.contest_id,
               settings.direct_qualifier_count,
               settings.elimination_qualifier_count
        FROM swiss_stage_prediction_settings AS settings
        JOIN contests ON contests.id = settings.contest_id
        WHERE contests.template_key = ?
          AND (
              settings.direct_qualifier_count != ?
              OR settings.elimination_qualifier_count != ?
          )
        ORDER BY settings.contest_id
        """,
        (
            CHAMPIONS_LEAGUE_TEMPLATE_KEY,
            CHAMPIONS_LEAGUE_DIRECT_COUNT,
            CHAMPIONS_LEAGUE_ELIMINATION_COUNT,
        ),
    ).fetchall()
    normalized = 0
    for row in rows:
        contest_id = int(row["contest_id"])
        counts = (
            int(row["direct_qualifier_count"]),
            int(row["elimination_qualifier_count"]),
        )
        if counts != (CHAMPIONS_LEAGUE_DIRECT_COUNT, 16):
            raise RuntimeError(
                f"Champions League contest {contest_id} has unexpected limits "
                f"{counts[0]}+{counts[1]}; refusing to migrate it."
            )
        if _local_stage_has_data(connection, contest_id=contest_id):
            raise RuntimeError(
                f"Champions League contest {contest_id} has legacy 8+16 limits "
                "and predictions or results; manual repair is required."
            )
        connection.execute(
            """
            UPDATE swiss_stage_prediction_settings
            SET elimination_qualifier_count = ?
            WHERE contest_id = ?
            """,
            (CHAMPIONS_LEAGUE_ELIMINATION_COUNT, contest_id),
        )
        normalized += 1
    return normalized


def _normalize_unlocked_legacy_shared_limits(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        """
        SELECT settings.shared_tournament_id,
               settings.swiss_direct_qualifier_count,
               settings.swiss_elimination_qualifier_count
        FROM shared_tournament_settings AS settings
        JOIN shared_tournaments AS tournaments
          ON tournaments.id = settings.shared_tournament_id
        WHERE tournaments.template_key = ?
          AND (
              settings.swiss_direct_qualifier_count != ?
              OR settings.swiss_elimination_qualifier_count != ?
          )
        ORDER BY settings.shared_tournament_id
        """,
        (
            CHAMPIONS_LEAGUE_TEMPLATE_KEY,
            CHAMPIONS_LEAGUE_DIRECT_COUNT,
            CHAMPIONS_LEAGUE_ELIMINATION_COUNT,
        ),
    ).fetchall()
    normalized = 0
    for row in rows:
        shared_tournament_id = int(row["shared_tournament_id"])
        counts = (
            int(row["swiss_direct_qualifier_count"]),
            int(row["swiss_elimination_qualifier_count"]),
        )
        if counts != (CHAMPIONS_LEAGUE_DIRECT_COUNT, 16):
            raise RuntimeError(
                "Champions League shared tournament "
                f"{shared_tournament_id} has unexpected limits "
                f"{counts[0]}+{counts[1]}; refusing to migrate it."
            )
        if _shared_stage_has_data(
            connection, shared_tournament_id=shared_tournament_id
        ):
            raise RuntimeError(
                "Champions League shared tournament "
                f"{shared_tournament_id} has legacy 8+16 limits and predictions "
                "or results; manual repair is required."
            )
        connection.execute(
            """
            UPDATE shared_tournament_settings
            SET swiss_elimination_qualifier_count = ?
            WHERE shared_tournament_id = ?
            """,
            (CHAMPIONS_LEAGUE_ELIMINATION_COUNT, shared_tournament_id),
        )
        normalized += 1
    return normalized


def _local_stage_has_data(connection: sqlite3.Connection, *, contest_id: int) -> bool:
    row = connection.execute(
        """
        SELECT
            EXISTS (
                SELECT 1 FROM swiss_stage_predictions
                WHERE contest_id = ?
            )
            OR EXISTS (
                SELECT 1 FROM swiss_stage_results
                WHERE contest_id = ?
            )
            OR EXISTS (
                SELECT 1
                FROM contest_shared_tournaments AS links
                JOIN shared_swiss_stage_result_selections AS selections
                  ON selections.shared_tournament_id = links.shared_tournament_id
                WHERE links.contest_id = ?
            ) AS value
        """,
        (contest_id, contest_id, contest_id),
    ).fetchone()
    return bool(row["value"])


def _shared_stage_has_data(
    connection: sqlite3.Connection, *, shared_tournament_id: int
) -> bool:
    row = connection.execute(
        """
        SELECT
            EXISTS (
                SELECT 1 FROM shared_swiss_stage_result_selections
                WHERE shared_tournament_id = ?
            )
            OR EXISTS (
                SELECT 1
                FROM contest_shared_tournaments AS links
                JOIN swiss_stage_predictions AS predictions
                  ON predictions.contest_id = links.contest_id
                WHERE links.shared_tournament_id = ?
            )
            OR EXISTS (
                SELECT 1
                FROM contest_shared_tournaments AS links
                JOIN swiss_stage_results AS results
                  ON results.contest_id = links.contest_id
                WHERE links.shared_tournament_id = ?
            ) AS value
        """,
        (shared_tournament_id, shared_tournament_id, shared_tournament_id),
    ).fetchone()
    return bool(row["value"])


def _backfill_champions_league_policy(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE swiss_stage_prediction_settings
        SET selection_mode = 'up_to_limits',
            direct_correct_points = 2,
            elimination_correct_points = 1,
            cross_category_points = 0
        WHERE contest_id IN (
            SELECT id FROM contests WHERE template_key = ?
        )
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    )
    connection.execute(
        """
        UPDATE shared_tournament_settings
        SET swiss_selection_mode = 'up_to_limits',
            swiss_direct_correct_points = 2,
            swiss_elimination_correct_points = 1,
            swiss_cross_category_points = 0
        WHERE shared_tournament_id IN (
            SELECT id FROM shared_tournaments WHERE template_key = ?
        )
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    )


def _audit_general_stage_data(
    connection: sqlite3.Connection,
    *,
    normalized_local_count: int,
    normalized_shared_count: int,
    now_utc: datetime,
) -> MigrationReport:
    _require_champions_league_settings(connection)
    _require_champions_league_team_counts(connection)
    _require_valid_predictions(connection)
    _require_exact_local_results(connection)
    _require_exact_shared_results(connection)
    _require_consistent_links(connection, now_utc=now_utc)

    return MigrationReport(
        champions_league_contest_count=_scalar_count(
            connection,
            "SELECT COUNT(*) FROM contests WHERE template_key = ?",
            (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
        ),
        champions_league_shared_tournament_count=_scalar_count(
            connection,
            "SELECT COUNT(*) FROM shared_tournaments WHERE template_key = ?",
            (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
        ),
        prediction_count=_scalar_count(
            connection,
            """
            SELECT COUNT(*)
            FROM swiss_stage_predictions AS predictions
            JOIN contests ON contests.id = predictions.contest_id
            WHERE contests.template_key = ?
            """,
            (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
        ),
        local_result_count=_scalar_count(
            connection,
            """
            SELECT COUNT(*)
            FROM swiss_stage_results AS results
            JOIN contests ON contests.id = results.contest_id
            WHERE contests.template_key = ?
            """,
            (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
        ),
        shared_result_count=_scalar_count(
            connection,
            """
            SELECT COUNT(DISTINCT selections.shared_tournament_id)
            FROM shared_swiss_stage_result_selections AS selections
            JOIN shared_tournaments AS tournaments
              ON tournaments.id = selections.shared_tournament_id
            WHERE tournaments.template_key = ?
            """,
            (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
        ),
        normalized_local_legacy_limit_count=normalized_local_count,
        normalized_shared_legacy_limit_count=normalized_shared_count,
    )


def _require_champions_league_settings(connection: sqlite3.Connection) -> None:
    missing_local = connection.execute(
        """
        SELECT contests.id
        FROM contests
        LEFT JOIN swiss_stage_prediction_settings AS settings
          ON settings.contest_id = contests.id
        WHERE contests.template_key = ? AND settings.contest_id IS NULL
        LIMIT 1
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchone()
    if missing_local is not None:
        raise RuntimeError(
            "Champions League contest "
            f"{int(missing_local['id'])} has no general-stage settings."
        )

    invalid_local = connection.execute(
        """
        SELECT settings.contest_id
        FROM swiss_stage_prediction_settings AS settings
        JOIN contests ON contests.id = settings.contest_id
        WHERE contests.template_key = ?
          AND (
              settings.direct_qualifier_count != 8
              OR settings.elimination_qualifier_count != 12
              OR settings.selection_mode != 'up_to_limits'
              OR settings.direct_correct_points != 2
              OR settings.elimination_correct_points != 1
              OR settings.cross_category_points != 0
          )
        LIMIT 1
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchone()
    if invalid_local is not None:
        raise RuntimeError(
            "Champions League contest "
            f"{int(invalid_local['contest_id'])} has invalid general-stage policy."
        )

    missing_shared = connection.execute(
        """
        SELECT tournaments.id
        FROM shared_tournaments AS tournaments
        LEFT JOIN shared_tournament_settings AS settings
          ON settings.shared_tournament_id = tournaments.id
        WHERE tournaments.template_key = ?
          AND settings.shared_tournament_id IS NULL
        LIMIT 1
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchone()
    if missing_shared is not None:
        raise RuntimeError(
            "Champions League shared tournament "
            f"{int(missing_shared['id'])} has no general-stage settings."
        )

    invalid_shared = connection.execute(
        """
        SELECT settings.shared_tournament_id
        FROM shared_tournament_settings AS settings
        JOIN shared_tournaments AS tournaments
          ON tournaments.id = settings.shared_tournament_id
        WHERE tournaments.template_key = ?
          AND (
              settings.swiss_direct_qualifier_count != 8
              OR settings.swiss_elimination_qualifier_count != 12
              OR settings.swiss_selection_mode != 'up_to_limits'
              OR settings.swiss_direct_correct_points != 2
              OR settings.swiss_elimination_correct_points != 1
              OR settings.swiss_cross_category_points != 0
          )
        LIMIT 1
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchone()
    if invalid_shared is not None:
        raise RuntimeError(
            "Champions League shared tournament "
            f"{int(invalid_shared['shared_tournament_id'])} has invalid "
            "general-stage policy."
        )


def _require_champions_league_team_counts(connection: sqlite3.Connection) -> None:
    invalid_local = connection.execute(
        """
        SELECT contests.id, COUNT(contest_teams.team_id) AS team_count
        FROM contests
        LEFT JOIN contest_teams ON contest_teams.contest_id = contests.id
        WHERE contests.template_key = ?
        GROUP BY contests.id
        HAVING COUNT(contest_teams.team_id) != 36
        LIMIT 1
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchone()
    if invalid_local is not None:
        raise RuntimeError(
            "Champions League contest "
            f"{int(invalid_local['id'])} has {int(invalid_local['team_count'])} "
            "teams instead of 36."
        )
    invalid_shared = connection.execute(
        """
        SELECT tournaments.id, COUNT(teams.team_id) AS team_count
        FROM shared_tournaments AS tournaments
        LEFT JOIN shared_tournament_teams AS teams
          ON teams.shared_tournament_id = tournaments.id
        WHERE tournaments.template_key = ?
        GROUP BY tournaments.id
        HAVING COUNT(teams.team_id) != 36
        LIMIT 1
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchone()
    if invalid_shared is not None:
        raise RuntimeError(
            "Champions League shared tournament "
            f"{int(invalid_shared['id'])} has "
            f"{int(invalid_shared['team_count'])} teams instead of 36."
        )


def _require_valid_predictions(connection: sqlite3.Connection) -> None:
    invalid = connection.execute(
        """
        SELECT predictions.id,
               SUM(CASE WHEN selections.category = 'direct' THEN 1 ELSE 0 END)
                 AS direct_count,
               SUM(CASE WHEN selections.category = 'elimination' THEN 1 ELSE 0 END)
                 AS elimination_count,
               SUM(CASE
                       WHEN selections.team_id IS NOT NULL
                        AND selections.category NOT IN ('direct', 'elimination')
                       THEN 1 ELSE 0
                   END) AS invalid_category_count,
               SUM(CASE
                       WHEN selections.team_id IS NOT NULL
                        AND contest_teams.team_id IS NULL
                       THEN 1 ELSE 0
                   END) AS foreign_team_count,
               COUNT(selections.team_id) AS selected_count,
               COUNT(DISTINCT selections.team_id) AS distinct_team_count
        FROM swiss_stage_predictions AS predictions
        JOIN contests ON contests.id = predictions.contest_id
        LEFT JOIN swiss_stage_prediction_selections AS selections
          ON selections.prediction_id = predictions.id
         AND selections.contest_id = predictions.contest_id
        LEFT JOIN contest_teams
          ON contest_teams.contest_id = predictions.contest_id
         AND contest_teams.team_id = selections.team_id
        WHERE contests.template_key = ?
        GROUP BY predictions.id
        HAVING direct_count > 8
            OR elimination_count > 12
            OR invalid_category_count > 0
            OR foreign_team_count > 0
            OR selected_count != distinct_team_count
        LIMIT 1
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchone()
    if invalid is not None:
        raise RuntimeError(
            "Champions League prediction "
            f"{int(invalid['id'])} is malformed or exceeds 8+12 limits."
        )


def _require_exact_local_results(connection: sqlite3.Connection) -> None:
    invalid = connection.execute(
        """
        SELECT results.contest_id,
               SUM(CASE WHEN selections.category = 'direct' THEN 1 ELSE 0 END)
                 AS direct_count,
               SUM(CASE WHEN selections.category = 'elimination' THEN 1 ELSE 0 END)
                 AS elimination_count,
               SUM(CASE
                       WHEN selections.team_id IS NOT NULL
                        AND selections.category NOT IN ('direct', 'elimination')
                       THEN 1 ELSE 0
                   END) AS invalid_category_count,
               SUM(CASE
                       WHEN selections.team_id IS NOT NULL
                        AND contest_teams.team_id IS NULL
                       THEN 1 ELSE 0
                   END) AS foreign_team_count,
               COUNT(selections.team_id) AS selected_count,
               COUNT(DISTINCT selections.team_id) AS distinct_team_count
        FROM swiss_stage_results AS results
        JOIN contests ON contests.id = results.contest_id
        LEFT JOIN swiss_stage_result_selections AS selections
          ON selections.contest_id = results.contest_id
        LEFT JOIN contest_teams
          ON contest_teams.contest_id = results.contest_id
         AND contest_teams.team_id = selections.team_id
        WHERE contests.template_key = ?
        GROUP BY results.contest_id
        HAVING direct_count != 8
            OR elimination_count != 12
            OR invalid_category_count > 0
            OR foreign_team_count > 0
            OR selected_count != distinct_team_count
        LIMIT 1
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchone()
    if invalid is not None:
        raise RuntimeError(
            "Champions League result for contest "
            f"{int(invalid['contest_id'])} is not an exact 8+12 selection."
        )


def _require_exact_shared_results(connection: sqlite3.Connection) -> None:
    invalid = connection.execute(
        """
        SELECT tournaments.id,
               SUM(CASE WHEN selections.category = 'direct' THEN 1 ELSE 0 END)
                 AS direct_count,
               SUM(CASE WHEN selections.category = 'elimination' THEN 1 ELSE 0 END)
                 AS elimination_count,
               SUM(CASE
                       WHEN selections.team_id IS NOT NULL
                        AND selections.category NOT IN ('direct', 'elimination')
                       THEN 1 ELSE 0
                   END) AS invalid_category_count,
               SUM(CASE
                       WHEN selections.team_id IS NOT NULL
                        AND shared_teams.team_id IS NULL
                       THEN 1 ELSE 0
                   END) AS foreign_team_count,
               COUNT(selections.team_id) AS selected_count,
               COUNT(DISTINCT selections.team_id) AS distinct_team_count
        FROM shared_tournaments AS tournaments
        JOIN shared_swiss_stage_result_selections AS selections
          ON selections.shared_tournament_id = tournaments.id
        LEFT JOIN shared_tournament_teams AS shared_teams
          ON shared_teams.shared_tournament_id = tournaments.id
         AND shared_teams.team_id = selections.team_id
        WHERE tournaments.template_key = ?
        GROUP BY tournaments.id
        HAVING direct_count != 8
            OR elimination_count != 12
            OR invalid_category_count > 0
            OR foreign_team_count > 0
            OR selected_count != distinct_team_count
        LIMIT 1
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchone()
    if invalid is not None:
        raise RuntimeError(
            "Champions League result for shared tournament "
            f"{int(invalid['id'])} is not an exact 8+12 selection."
        )


def _require_consistent_links(
    connection: sqlite3.Connection, *, now_utc: datetime
) -> None:
    template_mismatch = connection.execute(
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
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY, CHAMPIONS_LEAGUE_TEMPLATE_KEY),
    ).fetchone()
    if template_mismatch is not None:
        raise RuntimeError("Champions League shared link has mismatched templates.")

    settings_mismatch = connection.execute(
        """
        SELECT links.contest_id, links.shared_tournament_id
        FROM contest_shared_tournaments AS links
        JOIN contests ON contests.id = links.contest_id
        JOIN shared_tournaments AS tournaments
          ON tournaments.id = links.shared_tournament_id
        LEFT JOIN swiss_stage_prediction_settings AS local
          ON local.contest_id = links.contest_id
        LEFT JOIN shared_tournament_settings AS shared
          ON shared.shared_tournament_id = links.shared_tournament_id
        WHERE contests.template_key = ?
          AND (
              local.contest_id IS NULL
              OR shared.shared_tournament_id IS NULL
              OR local.enabled != shared.swiss_stage_prediction_enabled
              OR local.direct_qualifier_count != shared.swiss_direct_qualifier_count
              OR local.elimination_qualifier_count !=
                 shared.swiss_elimination_qualifier_count
              OR local.selection_mode != shared.swiss_selection_mode
              OR local.direct_correct_points != shared.swiss_direct_correct_points
              OR local.elimination_correct_points !=
                 shared.swiss_elimination_correct_points
              OR local.cross_category_points != shared.swiss_cross_category_points
          )
        LIMIT 1
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchone()
    if settings_mismatch is not None:
        raise RuntimeError(
            "Champions League linked general-stage settings are inconsistent for "
            f"contest {int(settings_mismatch['contest_id'])} and shared tournament "
            f"{int(settings_mismatch['shared_tournament_id'])}."
        )

    deadline_mismatches = connection.execute(
        """
        SELECT links.contest_id, links.shared_tournament_id,
               local.deadline_at AS local_deadline_at,
               shared.swiss_stage_prediction_deadline_at AS shared_deadline_at
        FROM contest_shared_tournaments AS links
        JOIN contests ON contests.id = links.contest_id
        JOIN swiss_stage_prediction_settings AS local
          ON local.contest_id = links.contest_id
        JOIN shared_tournament_settings AS shared
          ON shared.shared_tournament_id = links.shared_tournament_id
        WHERE contests.template_key = ?
          AND local.deadline_at IS NOT shared.swiss_stage_prediction_deadline_at
        ORDER BY links.contest_id
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchall()
    for mismatch in deadline_mismatches:
        local_deadline = mismatch["local_deadline_at"]
        shared_deadline = mismatch["shared_deadline_at"]
        if (
            local_deadline is None
            or shared_deadline is None
            or _parse_datetime(str(local_deadline)) > now_utc
            or _parse_datetime(str(shared_deadline)) > now_utc
        ):
            raise RuntimeError(
                "Champions League linked open general-stage deadlines are "
                "inconsistent for contest "
                f"{int(mismatch['contest_id'])} and shared tournament "
                f"{int(mismatch['shared_tournament_id'])}."
            )

    result_mismatch = connection.execute(
        """
        SELECT links.contest_id, links.shared_tournament_id
        FROM contest_shared_tournaments AS links
        JOIN contests ON contests.id = links.contest_id
        WHERE contests.template_key = ?
          AND (
              EXISTS (
                  SELECT 1 FROM swiss_stage_results AS local_result
                  WHERE local_result.contest_id = links.contest_id
              ) != EXISTS (
                  SELECT 1
                  FROM shared_swiss_stage_result_selections AS shared_selection
                  WHERE shared_selection.shared_tournament_id =
                        links.shared_tournament_id
              )
              OR EXISTS (
                  SELECT local_selection.team_id, local_selection.category
                  FROM swiss_stage_result_selections AS local_selection
                  WHERE local_selection.contest_id = links.contest_id
                  EXCEPT
                  SELECT shared_selection.team_id, shared_selection.category
                  FROM shared_swiss_stage_result_selections AS shared_selection
                  WHERE shared_selection.shared_tournament_id =
                        links.shared_tournament_id
              )
              OR EXISTS (
                  SELECT shared_selection.team_id, shared_selection.category
                  FROM shared_swiss_stage_result_selections AS shared_selection
                  WHERE shared_selection.shared_tournament_id =
                        links.shared_tournament_id
                  EXCEPT
                  SELECT local_selection.team_id, local_selection.category
                  FROM swiss_stage_result_selections AS local_selection
                  WHERE local_selection.contest_id = links.contest_id
              )
          )
        LIMIT 1
        """,
        (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
    ).fetchone()
    if result_mismatch is not None:
        raise RuntimeError(
            "Champions League linked general-stage results are inconsistent for "
            f"contest {int(result_mismatch['contest_id'])} and shared tournament "
            f"{int(result_mismatch['shared_tournament_id'])}."
        )


def _require_expected_schema(connection: sqlite3.Connection) -> None:
    for table_name, expected_columns in (
        ("swiss_stage_prediction_settings", tuple(_LOCAL_POLICY_COLUMNS)),
        ("shared_tournament_settings", tuple(_SHARED_POLICY_COLUMNS)),
    ):
        if (
            _policy_schema_state(
                connection,
                table_name=table_name,
                expected_columns=expected_columns,
            )
            != "current"
        ):
            raise RuntimeError(f"General-stage migration did not update {table_name}.")


def _column_names(connection: sqlite3.Connection, table_name: str) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
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


def _scalar_count(
    connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> int:
    row = connection.execute(sql, parameters).fetchone()
    if row is None:
        raise RuntimeError("Could not count general-stage rows.")
    return int(row[0])


def _require_healthy_database(connection: sqlite3.Connection, *, phase: str) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise RuntimeError(f"Database integrity check failed {phase}.")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"Foreign key check failed {phase}: {foreign_key_errors!r}")


def _resolve_now(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware.")
    return resolved.astimezone(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"Invalid general-stage deadline: {value!r}.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"General-stage deadline must include timezone: {value!r}.")
    return parsed.astimezone(timezone.utc)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Add configurable general-stage prediction rules and migrate "
            "Champions League 2026/27 to partial 8+12 selections."
        )
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
    report = migrate_database(arguments.database_path)
    print(f"Backup: {backup_path}")
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    print(f"Migration completed: {arguments.database_path}")


if __name__ == "__main__":
    main()
