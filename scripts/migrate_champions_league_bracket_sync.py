from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_PRESERVED_TABLES = (
    "stages",
    "shared_two_legged_ties",
    "shared_matches",
    "ties",
    "matches",
    "match_predictions",
    "tie_predictions",
    "match_prediction_scores",
    "tie_prediction_scores",
)

_NEW_TABLES = (
    "shared_bracket_nodes",
    "shared_tournament_external_sources",
    "shared_team_external_links",
    "shared_tie_external_links",
    "shared_fixture_imports",
)

_NEW_INDEXES = (
    "idx_stages_competition_stage_key",
    "idx_shared_two_legged_ties_bracket_position",
    "idx_shared_matches_standalone_bracket_position",
    "idx_shared_bracket_nodes_first_source",
    "idx_shared_bracket_nodes_second_source",
    "idx_shared_tie_external_links_live_tie",
    "idx_shared_fixture_imports_pending",
)

_NEW_TRIGGERS = (
    "trg_shared_matches_fixture_import_tombstone",
    "trg_shared_ties_external_link_tombstone",
)


def migrate_database(database_path: Path) -> None:
    if not database_path.is_file():
        raise RuntimeError(f"Database file does not exist: {database_path}")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _require_base_schema(connection)
        _require_compatible_existing_new_tables(connection)
        _require_healthy_database(connection, phase="before migration")
        preserved_counts = _table_counts(connection, _PRESERVED_TABLES)

        connection.execute("BEGIN IMMEDIATE")
        try:
            _add_columns(connection)
            for statement in _schema_statements():
                connection.execute(statement)
            _require_expected_schema(connection)
            _require_counts_unchanged(connection, preserved_counts)
            _require_healthy_database(connection, phase="during migration")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        _require_expected_schema(connection)
        _require_counts_unchanged(connection, preserved_counts)
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


def _add_columns(connection: sqlite3.Connection) -> None:
    if "stage_key" not in _column_names(connection, "stages"):
        connection.execute(
            """
            ALTER TABLE stages ADD COLUMN stage_key TEXT CHECK (
                stage_key IS NULL OR stage_key IN (
                    'playoff', 'round_of_16', 'quarterfinal', 'semifinal', 'final'
                )
            )
            """
        )
    if "round_key" not in _column_names(connection, "shared_two_legged_ties"):
        connection.execute(
            """
            ALTER TABLE shared_two_legged_ties ADD COLUMN round_key TEXT CHECK (
                round_key IS NULL OR round_key IN (
                    'playoff', 'round_of_16', 'quarterfinal', 'semifinal'
                )
            )
            """
        )
    if "bracket_position" not in _column_names(connection, "shared_two_legged_ties"):
        connection.execute(
            """
            ALTER TABLE shared_two_legged_ties
            ADD COLUMN bracket_position INTEGER CHECK (
                bracket_position IS NULL OR bracket_position > 0
            )
            """
        )
    if "round_key" not in _column_names(connection, "shared_matches"):
        connection.execute(
            """
            ALTER TABLE shared_matches ADD COLUMN round_key TEXT CHECK (
                round_key IS NULL OR round_key IN (
                    'playoff', 'round_of_16', 'quarterfinal', 'semifinal', 'final'
                )
            )
            """
        )
    if "bracket_position" not in _column_names(connection, "shared_matches"):
        connection.execute(
            """
            ALTER TABLE shared_matches ADD COLUMN bracket_position INTEGER CHECK (
                bracket_position IS NULL OR bracket_position > 0
            )
            """
        )
    if _schema_object_exists(
        connection, "shared_fixture_imports", object_type="table"
    ) and "version" not in _column_names(connection, "shared_fixture_imports"):
        connection.execute(
            """
            ALTER TABLE shared_fixture_imports
            ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
            """
        )
    if _schema_object_exists(
        connection, "shared_tournament_external_sources", object_type="table"
    ) and "sync_generation" not in _column_names(
        connection, "shared_tournament_external_sources"
    ):
        connection.execute(
            """
            ALTER TABLE shared_tournament_external_sources
            ADD COLUMN sync_generation INTEGER NOT NULL DEFAULT 1
                CHECK (sync_generation > 0)
            """
        )
    if _schema_object_exists(
        connection, "shared_tie_external_links", object_type="table"
    ):
        tie_link_columns = _column_names(connection, "shared_tie_external_links")
        if "round_key" not in tie_link_columns:
            connection.execute(
                """
                ALTER TABLE shared_tie_external_links ADD COLUMN round_key TEXT CHECK (
                    round_key IS NULL OR round_key IN (
                        'playoff', 'round_of_16', 'quarterfinal', 'semifinal'
                    )
                )
                """
            )
        if "bracket_position" not in tie_link_columns:
            connection.execute(
                """
                ALTER TABLE shared_tie_external_links
                ADD COLUMN bracket_position INTEGER CHECK (
                    bracket_position IS NULL OR bracket_position > 0
                )
                """
            )
        if "materialization_claim" not in tie_link_columns:
            connection.execute(
                "ALTER TABLE shared_tie_external_links "
                "ADD COLUMN materialization_claim TEXT"
            )
        if "claim_started_at" not in tie_link_columns:
            connection.execute(
                "ALTER TABLE shared_tie_external_links ADD COLUMN claim_started_at TEXT"
            )


def _schema_statements() -> tuple[str, ...]:
    return (
        "DROP TRIGGER IF EXISTS trg_shared_matches_fixture_import_tombstone",
        "DROP TRIGGER IF EXISTS trg_shared_ties_external_link_tombstone",
        "DROP INDEX IF EXISTS idx_stages_competition_stage_key",
        "DROP INDEX IF EXISTS idx_shared_two_legged_ties_bracket_position",
        "DROP INDEX IF EXISTS idx_shared_matches_standalone_bracket_position",
        "DROP INDEX IF EXISTS idx_shared_bracket_nodes_first_source",
        "DROP INDEX IF EXISTS idx_shared_bracket_nodes_second_source",
        "DROP INDEX IF EXISTS idx_shared_tie_external_links_live_tie",
        "DROP INDEX IF EXISTS idx_shared_fixture_imports_pending",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stages_competition_stage_key
        ON stages(competition_id, stage_key)
        WHERE stage_key IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_two_legged_ties_bracket_position
        ON shared_two_legged_ties(
            shared_tournament_id, round_key, bracket_position
        )
        WHERE round_key IS NOT NULL AND bracket_position IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_matches_standalone_bracket_position
        ON shared_matches(shared_tournament_id, round_key, bracket_position)
        WHERE shared_tie_id IS NULL
          AND round_key IS NOT NULL
          AND bracket_position IS NOT NULL
        """,
        """
        CREATE TABLE IF NOT EXISTS shared_bracket_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shared_tournament_id INTEGER NOT NULL
                REFERENCES shared_tournaments(id) ON DELETE CASCADE,
            round_key TEXT NOT NULL CHECK (
                round_key IN (
                    'playoff', 'round_of_16', 'quarterfinal', 'semifinal', 'final'
                )
            ),
            bracket_position INTEGER NOT NULL CHECK (bracket_position > 0),
            node_format TEXT NOT NULL CHECK (
                node_format IN ('two_legged', 'single')
            ),
            first_source_node_id INTEGER
                REFERENCES shared_bracket_nodes(id) ON DELETE SET NULL,
            second_source_node_id INTEGER
                REFERENCES shared_bracket_nodes(id) ON DELETE SET NULL,
            resolved_first_team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
            resolved_second_team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
            first_leg_starts_at_utc TEXT,
            second_leg_starts_at_utc TEXT,
            materialized_shared_tie_id INTEGER UNIQUE
                REFERENCES shared_two_legged_ties(id) ON DELETE SET NULL,
            materialized_shared_match_id INTEGER UNIQUE
                REFERENCES shared_matches(id) ON DELETE SET NULL,
            sync_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                sync_status IN ('pending', 'materialized', 'conflict')
            ),
            sync_error TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (shared_tournament_id, round_key, bracket_position),
            CHECK (
                (round_key = 'final' AND node_format = 'single')
                OR (round_key != 'final' AND node_format = 'two_legged')
            ),
            CHECK (
                first_source_node_id IS NULL
                OR second_source_node_id IS NULL
                OR first_source_node_id != second_source_node_id
            ),
            CHECK (
                resolved_first_team_id IS NULL
                OR resolved_second_team_id IS NULL
                OR resolved_first_team_id != resolved_second_team_id
            ),
            CHECK (
                (node_format = 'two_legged' AND materialized_shared_match_id IS NULL)
                OR (node_format = 'single' AND materialized_shared_tie_id IS NULL)
            ),
            CHECK (node_format = 'two_legged' OR second_leg_starts_at_utc IS NULL)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_shared_bracket_nodes_first_source
        ON shared_bracket_nodes(first_source_node_id)
        WHERE first_source_node_id IS NOT NULL
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_shared_bracket_nodes_second_source
        ON shared_bracket_nodes(second_source_node_id)
        WHERE second_source_node_id IS NOT NULL
        """,
        """
        CREATE TABLE IF NOT EXISTS shared_tournament_external_sources (
            shared_tournament_id INTEGER NOT NULL
                REFERENCES shared_tournaments(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            external_event_id TEXT NOT NULL,
            sync_enabled INTEGER NOT NULL DEFAULT 0 CHECK (sync_enabled IN (0, 1)),
            enabled_at TEXT,
            sync_generation INTEGER NOT NULL DEFAULT 1 CHECK (sync_generation > 0),
            last_attempt_at TEXT,
            last_success_at TEXT,
            last_error TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (shared_tournament_id, source)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS shared_team_external_links (
            shared_tournament_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            external_team_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (shared_tournament_id, source, external_team_id),
            UNIQUE (shared_tournament_id, source, team_id),
            FOREIGN KEY (shared_tournament_id, team_id)
                REFERENCES shared_tournament_teams(shared_tournament_id, team_id)
                ON DELETE CASCADE,
            FOREIGN KEY (shared_tournament_id, source)
                REFERENCES shared_tournament_external_sources(
                    shared_tournament_id, source
                ) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS shared_tie_external_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shared_tournament_id INTEGER NOT NULL
                REFERENCES shared_tournaments(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            external_event_id TEXT NOT NULL,
            external_tie_id TEXT NOT NULL,
            shared_tie_id INTEGER
                REFERENCES shared_two_legged_ties(id) ON DELETE SET NULL,
            round_key TEXT CHECK (
                round_key IS NULL
                OR round_key IN (
                    'playoff', 'round_of_16', 'quarterfinal', 'semifinal'
                )
            ),
            bracket_position INTEGER CHECK (
                bracket_position IS NULL OR bracket_position > 0
            ),
            materialization_claim TEXT,
            claim_started_at TEXT,
            first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            tombstoned_at TEXT,
            UNIQUE (
                shared_tournament_id, source, external_event_id, external_tie_id
            ),
            CHECK (
                (materialization_claim IS NULL) = (claim_started_at IS NULL)
            ),
            CHECK (
                (round_key IS NULL) = (bracket_position IS NULL)
            )
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_tie_external_links_live_tie
        ON shared_tie_external_links(shared_tie_id)
        WHERE shared_tie_id IS NOT NULL
        """,
        """
        CREATE TABLE IF NOT EXISTS shared_fixture_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shared_tournament_id INTEGER NOT NULL
                REFERENCES shared_tournaments(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            external_event_id TEXT NOT NULL,
            external_fixture_id TEXT NOT NULL,
            external_tie_id TEXT,
            round_key TEXT NOT NULL CHECK (
                round_key IN (
                    'playoff', 'round_of_16', 'quarterfinal', 'semifinal', 'final'
                )
            ),
            bracket_position INTEGER NOT NULL CHECK (bracket_position > 0),
            leg_number INTEGER CHECK (leg_number IS NULL OR leg_number IN (1, 2)),
            shared_bracket_node_id INTEGER
                REFERENCES shared_bracket_nodes(id) ON DELETE SET NULL,
            shared_tie_id INTEGER
                REFERENCES shared_two_legged_ties(id) ON DELETE SET NULL,
            shared_match_id INTEGER
                REFERENCES shared_matches(id) ON DELETE SET NULL,
            payload_hash TEXT NOT NULL,
            provider_updated_at TEXT,
            import_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                import_status IN ('pending', 'imported', 'conflict', 'tombstoned')
            ),
            last_error TEXT,
            first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            imported_at TEXT,
            tombstoned_at TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            UNIQUE (
                shared_tournament_id, source, external_event_id,
                external_fixture_id
            ),
            CHECK (
                (round_key = 'final' AND leg_number IS NULL)
                OR (round_key != 'final' AND leg_number IN (1, 2))
            )
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_shared_fixture_imports_pending
        ON shared_fixture_imports(
            shared_tournament_id, import_status, last_seen_at
        )
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_shared_matches_fixture_import_tombstone
        BEFORE DELETE ON shared_matches
        BEGIN
            UPDATE shared_fixture_imports
            SET import_status = 'tombstoned',
                tombstoned_at = CURRENT_TIMESTAMP,
                last_error = 'Материализованный матч был удалён.',
                version = version + 1
            WHERE shared_match_id = OLD.id
               OR (
                    OLD.shared_tie_id IS NULL
                    AND shared_match_id IS NULL
                    AND shared_tie_id IS NULL
                    AND shared_tournament_id = OLD.shared_tournament_id
                    AND round_key = OLD.round_key
                    AND bracket_position = OLD.bracket_position
               );
            UPDATE shared_bracket_nodes
            SET sync_status = 'conflict',
                sync_error = 'Материализованный матч был удалён.',
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE materialized_shared_match_id = OLD.id
               OR (
                    OLD.shared_tie_id IS NULL
                    AND materialized_shared_match_id IS NULL
                    AND materialized_shared_tie_id IS NULL
                    AND shared_tournament_id = OLD.shared_tournament_id
                    AND round_key = OLD.round_key
                    AND bracket_position = OLD.bracket_position
               );
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_shared_ties_external_link_tombstone
        BEFORE DELETE ON shared_two_legged_ties
        BEGIN
            UPDATE shared_tie_external_links
            SET tombstoned_at = CURRENT_TIMESTAMP,
                last_seen_at = CURRENT_TIMESTAMP,
                materialization_claim = NULL,
                claim_started_at = NULL
            WHERE shared_tie_id = OLD.id
               OR (
                    shared_tie_id IS NULL
                    AND shared_tournament_id = OLD.shared_tournament_id
                    AND (
                        (
                            round_key = OLD.round_key
                            AND bracket_position = OLD.bracket_position
                        )
                        OR (
                            round_key IS NULL
                            AND bracket_position IS NULL
                            AND EXISTS (
                                SELECT 1
                                FROM shared_fixture_imports AS fallback_fixture
                                WHERE fallback_fixture.shared_tournament_id =
                                          shared_tie_external_links.shared_tournament_id
                                  AND fallback_fixture.source =
                                          shared_tie_external_links.source
                                  AND fallback_fixture.external_event_id =
                                          shared_tie_external_links.external_event_id
                                  AND fallback_fixture.external_tie_id =
                                          shared_tie_external_links.external_tie_id
                                  AND fallback_fixture.round_key = OLD.round_key
                                  AND fallback_fixture.bracket_position =
                                          OLD.bracket_position
                            )
                        )
                    )
               );
            UPDATE shared_fixture_imports
            SET import_status = 'tombstoned',
                tombstoned_at = CURRENT_TIMESTAMP,
                last_error = 'Материализованное противостояние было удалено.',
                version = version + 1
            WHERE shared_tie_id = OLD.id
               OR (
                    shared_tie_id IS NULL
                    AND shared_match_id IS NULL
                    AND shared_tournament_id = OLD.shared_tournament_id
                    AND round_key = OLD.round_key
                    AND bracket_position = OLD.bracket_position
               );
            UPDATE shared_bracket_nodes
            SET sync_status = 'conflict',
                sync_error = 'Материализованное противостояние было удалено.',
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE materialized_shared_tie_id = OLD.id
               OR (
                    materialized_shared_tie_id IS NULL
                    AND materialized_shared_match_id IS NULL
                    AND shared_tournament_id = OLD.shared_tournament_id
                    AND round_key = OLD.round_key
                    AND bracket_position = OLD.bracket_position
               );
        END
        """,
    )


def _require_base_schema(connection: sqlite3.Connection) -> None:
    required = {
        *_PRESERVED_TABLES,
        "shared_tournaments",
        "shared_tournament_teams",
        "teams",
    }
    missing = sorted(
        table_name
        for table_name in required
        if not _schema_object_exists(connection, table_name, object_type="table")
    )
    if missing:
        raise RuntimeError("Required tables are missing: " + ", ".join(missing))


def _require_compatible_existing_new_tables(connection: sqlite3.Connection) -> None:
    required_columns = {
        "shared_bracket_nodes": {
            "id",
            "shared_tournament_id",
            "round_key",
            "bracket_position",
            "node_format",
            "materialized_shared_tie_id",
            "materialized_shared_match_id",
        },
        "shared_tournament_external_sources": {
            "shared_tournament_id",
            "source",
            "external_event_id",
            "sync_enabled",
        },
        "shared_team_external_links": {
            "shared_tournament_id",
            "team_id",
            "source",
            "external_team_id",
        },
        "shared_tie_external_links": {
            "id",
            "shared_tournament_id",
            "source",
            "external_event_id",
            "external_tie_id",
            "shared_tie_id",
        },
        "shared_fixture_imports": {
            "id",
            "shared_tournament_id",
            "source",
            "external_event_id",
            "external_fixture_id",
            "round_key",
            "bracket_position",
            "shared_match_id",
        },
    }
    for table_name, required in required_columns.items():
        if not _schema_object_exists(connection, table_name, object_type="table"):
            continue
        missing = required.difference(_column_names(connection, table_name))
        if missing:
            raise RuntimeError(
                f"Preexisting migration table columns missing from {table_name}: "
                f"{sorted(missing)!r}"
            )


def _require_expected_schema(connection: sqlite3.Connection) -> None:
    required_columns = {
        "stages": {"stage_key"},
        "shared_two_legged_ties": {"round_key", "bracket_position"},
        "shared_matches": {"round_key", "bracket_position"},
        "shared_bracket_nodes": {
            "round_key",
            "bracket_position",
            "node_format",
            "first_source_node_id",
            "second_source_node_id",
            "resolved_first_team_id",
            "resolved_second_team_id",
            "first_leg_starts_at_utc",
            "second_leg_starts_at_utc",
            "materialized_shared_tie_id",
            "materialized_shared_match_id",
            "sync_status",
            "sync_error",
            "version",
        },
        "shared_tournament_external_sources": {
            "shared_tournament_id",
            "source",
            "external_event_id",
            "sync_enabled",
            "enabled_at",
            "sync_generation",
            "last_attempt_at",
            "last_success_at",
            "last_error",
            "version",
        },
        "shared_team_external_links": {
            "shared_tournament_id",
            "team_id",
            "source",
            "external_team_id",
        },
        "shared_tie_external_links": {
            "shared_tournament_id",
            "source",
            "external_event_id",
            "external_tie_id",
            "shared_tie_id",
            "round_key",
            "bracket_position",
            "tombstoned_at",
            "materialization_claim",
            "claim_started_at",
        },
        "shared_fixture_imports": {
            "shared_tournament_id",
            "source",
            "external_event_id",
            "external_fixture_id",
            "external_tie_id",
            "round_key",
            "bracket_position",
            "leg_number",
            "shared_bracket_node_id",
            "shared_tie_id",
            "shared_match_id",
            "payload_hash",
            "provider_updated_at",
            "import_status",
            "last_error",
            "imported_at",
            "tombstoned_at",
            "version",
        },
    }
    for table_name, expected in required_columns.items():
        missing = expected.difference(_column_names(connection, table_name))
        if missing:
            raise RuntimeError(
                f"Migration columns missing from {table_name}: {sorted(missing)!r}"
            )
    for object_type, names in (
        ("table", _NEW_TABLES),
        ("index", _NEW_INDEXES),
        ("trigger", _NEW_TRIGGERS),
    ):
        missing = [
            name
            for name in names
            if not _schema_object_exists(connection, name, object_type=object_type)
        ]
        if missing:
            raise RuntimeError(
                f"Migration {object_type}s are missing: {', '.join(missing)}"
            )
    for table_name, index_name, columns, unique, partial in (
        (
            "stages",
            "idx_stages_competition_stage_key",
            ("competition_id", "stage_key"),
            True,
            True,
        ),
        (
            "shared_two_legged_ties",
            "idx_shared_two_legged_ties_bracket_position",
            ("shared_tournament_id", "round_key", "bracket_position"),
            True,
            True,
        ),
        (
            "shared_matches",
            "idx_shared_matches_standalone_bracket_position",
            ("shared_tournament_id", "round_key", "bracket_position"),
            True,
            True,
        ),
        (
            "shared_bracket_nodes",
            "idx_shared_bracket_nodes_first_source",
            ("first_source_node_id",),
            False,
            True,
        ),
        (
            "shared_bracket_nodes",
            "idx_shared_bracket_nodes_second_source",
            ("second_source_node_id",),
            False,
            True,
        ),
        (
            "shared_tie_external_links",
            "idx_shared_tie_external_links_live_tie",
            ("shared_tie_id",),
            True,
            True,
        ),
        (
            "shared_fixture_imports",
            "idx_shared_fixture_imports_pending",
            ("shared_tournament_id", "import_status", "last_seen_at"),
            False,
            False,
        ),
    ):
        _require_index_shape(
            connection,
            table_name=table_name,
            index_name=index_name,
            expected_columns=columns,
            unique=unique,
            partial=partial,
        )
    _require_foreign_keys(
        connection,
        table_name="shared_bracket_nodes",
        expected={
            ("first_source_node_id", "shared_bracket_nodes", "SET NULL"),
            ("second_source_node_id", "shared_bracket_nodes", "SET NULL"),
            ("resolved_first_team_id", "teams", "SET NULL"),
            ("resolved_second_team_id", "teams", "SET NULL"),
            (
                "materialized_shared_tie_id",
                "shared_two_legged_ties",
                "SET NULL",
            ),
            ("materialized_shared_match_id", "shared_matches", "SET NULL"),
        },
    )
    _require_foreign_keys(
        connection,
        table_name="shared_tie_external_links",
        expected={("shared_tie_id", "shared_two_legged_ties", "SET NULL")},
    )
    _require_foreign_keys(
        connection,
        table_name="shared_fixture_imports",
        expected={
            ("shared_bracket_node_id", "shared_bracket_nodes", "SET NULL"),
            ("shared_tie_id", "shared_two_legged_ties", "SET NULL"),
            ("shared_match_id", "shared_matches", "SET NULL"),
        },
    )
    _require_trigger_fragments(
        connection,
        trigger_name="trg_shared_matches_fixture_import_tombstone",
        fragments=(
            "before delete on shared_matches",
            "where shared_match_id = old.id",
            "where materialized_shared_match_id = old.id",
            "shared_tournament_id = old.shared_tournament_id",
            "round_key = old.round_key",
            "bracket_position = old.bracket_position",
            "version = version + 1",
        ),
    )
    _require_trigger_fragments(
        connection,
        trigger_name="trg_shared_ties_external_link_tombstone",
        fragments=(
            "before delete on shared_two_legged_ties",
            "where shared_tie_id = old.id",
            "where materialized_shared_tie_id = old.id",
            "shared_tournament_id = old.shared_tournament_id",
            "round_key = old.round_key",
            "bracket_position = old.bracket_position",
            "version = version + 1",
        ),
    )
    _require_table_fragments(
        connection,
        table_name="shared_bracket_nodes",
        fragments=(
            "UNIQUE (shared_tournament_id, round_key, bracket_position)",
            "materialized_shared_tie_id INTEGER UNIQUE",
            "materialized_shared_match_id INTEGER UNIQUE",
            "round_key = 'final' AND node_format = 'single'",
        ),
    )
    _require_table_fragments(
        connection,
        table_name="shared_tournament_external_sources",
        fragments=(
            "PRIMARY KEY (shared_tournament_id, source)",
            "sync_generation INTEGER NOT NULL DEFAULT 1 CHECK (sync_generation > 0)",
        ),
        forbidden=("UNIQUE (source, external_event_id)",),
    )
    _require_table_fragments(
        connection,
        table_name="shared_team_external_links",
        fragments=(
            "PRIMARY KEY (shared_tournament_id, source, external_team_id)",
            "UNIQUE (shared_tournament_id, source, team_id)",
        ),
    )
    _require_table_fragments(
        connection,
        table_name="shared_tie_external_links",
        fragments=(
            "UNIQUE (shared_tournament_id, source, external_event_id, external_tie_id)",
        ),
    )
    _require_table_fragments(
        connection,
        table_name="shared_fixture_imports",
        fragments=(
            "UNIQUE (shared_tournament_id, source, external_event_id, external_fixture_id)",
            "round_key = 'final' AND leg_number IS NULL",
            "version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)",
        ),
    )


def _table_counts(
    connection: sqlite3.Connection, table_names: tuple[str, ...]
) -> dict[str, int]:
    return {
        table_name: int(
            connection.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
        )
        for table_name in table_names
    }


def _require_index_shape(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    index_name: str,
    expected_columns: tuple[str, ...],
    unique: bool,
    partial: bool,
) -> None:
    index_rows = connection.execute(f'PRAGMA index_list("{table_name}")').fetchall()
    index_row = next((row for row in index_rows if str(row[1]) == index_name), None)
    if index_row is None:
        raise RuntimeError(f"Migration index is missing: {index_name}")
    actual_columns = tuple(
        str(row[2])
        for row in connection.execute(f'PRAGMA index_info("{index_name}")').fetchall()
    )
    if (
        actual_columns != expected_columns
        or bool(index_row[2]) != unique
        or bool(index_row[4]) != partial
    ):
        raise RuntimeError(
            f"Migration index has an unexpected shape: {index_name}; "
            f"columns={actual_columns!r}, unique={bool(index_row[2])}, "
            f"partial={bool(index_row[4])}"
        )


def _require_foreign_keys(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    expected: set[tuple[str, str, str]],
) -> None:
    actual = {
        (str(row[3]), str(row[2]), str(row[6]).upper())
        for row in connection.execute(
            f'PRAGMA foreign_key_list("{table_name}")'
        ).fetchall()
    }
    missing = expected.difference(actual)
    if missing:
        raise RuntimeError(
            f"Migration foreign keys are missing from {table_name}: {missing!r}"
        )


def _require_trigger_fragments(
    connection: sqlite3.Connection,
    *,
    trigger_name: str,
    fragments: tuple[str, ...],
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (trigger_name,),
    ).fetchone()
    normalized_sql = " ".join(str(row[0]).lower().split()) if row is not None else ""
    missing = [fragment for fragment in fragments if fragment not in normalized_sql]
    if missing:
        raise RuntimeError(
            f"Migration trigger has an unexpected definition: {trigger_name}; "
            f"missing={missing!r}"
        )


def _require_table_fragments(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    fragments: tuple[str, ...],
    forbidden: tuple[str, ...] = (),
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    normalized_sql = "".join(str(row[0]).lower().split()) if row is not None else ""
    missing = [
        fragment
        for fragment in fragments
        if "".join(fragment.lower().split()) not in normalized_sql
    ]
    unexpected = [
        fragment
        for fragment in forbidden
        if "".join(fragment.lower().split()) in normalized_sql
    ]
    if missing or unexpected:
        raise RuntimeError(
            f"Migration table has an unexpected definition: {table_name}; "
            f"missing={missing!r}, forbidden={unexpected!r}"
        )


def _require_counts_unchanged(
    connection: sqlite3.Connection, expected_counts: dict[str, int]
) -> None:
    actual = _table_counts(connection, tuple(expected_counts))
    if actual != expected_counts:
        raise RuntimeError(
            "Historical row counts changed during bracket migration: "
            f"expected={expected_counts!r}, actual={actual!r}"
        )


def _column_names(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    }


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


def _require_healthy_database(connection: sqlite3.Connection, *, phase: str) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise RuntimeError(f"Database integrity check failed {phase}.")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"Foreign key check failed {phase}: {foreign_key_errors!r}")


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Add nullable Champions League round metadata, an empty bracket, "
            "and durable external-sync ledgers without backfilling results."
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
    migrate_database(arguments.database_path)
    print(f"Backup: {backup_path}")
    print(f"Migration completed: {arguments.database_path}")


if __name__ == "__main__":
    main()
