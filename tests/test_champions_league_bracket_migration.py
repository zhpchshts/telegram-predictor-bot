from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from scripts.migrate_champions_league_bracket_sync import (
    backup_database,
    migrate_database,
)


PRESERVED_TABLES = (
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


def _create_legacy_database(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE stages (
                id INTEGER PRIMARY KEY,
                competition_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                position INTEGER NOT NULL,
                stage_type TEXT NOT NULL
            );
            CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE shared_tournaments (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                template_key TEXT NOT NULL,
                is_archived INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE shared_tournament_teams (
                shared_tournament_id INTEGER NOT NULL,
                team_id INTEGER NOT NULL,
                PRIMARY KEY (shared_tournament_id, team_id)
            );
            CREATE TABLE shared_two_legged_ties (
                id INTEGER PRIMARY KEY,
                shared_tournament_id INTEGER NOT NULL
            );
            CREATE TABLE shared_matches (
                id INTEGER PRIMARY KEY,
                shared_tournament_id INTEGER NOT NULL,
                shared_tie_id INTEGER
            );
            CREATE TABLE ties (id INTEGER PRIMARY KEY);
            CREATE TABLE matches (id INTEGER PRIMARY KEY);
            CREATE TABLE match_predictions (id INTEGER PRIMARY KEY);
            CREATE TABLE tie_predictions (id INTEGER PRIMARY KEY);
            CREATE TABLE match_prediction_scores (id INTEGER PRIMARY KEY);
            CREATE TABLE tie_prediction_scores (id INTEGER PRIMARY KEY);

            INSERT INTO stages VALUES (1, 10, 'Исторический раунд', 1, 'knockout');
            INSERT INTO teams VALUES (1, 'Команда 1'), (2, 'Команда 2');
            INSERT INTO shared_tournaments VALUES (
                1, 'Историческая Лига чемпионов',
                'champions_league_2026_27', 0, 7
            );
            INSERT INTO shared_tournament_teams VALUES (1, 1), (1, 2);
            INSERT INTO shared_two_legged_ties VALUES (1, 1);
            INSERT INTO shared_matches VALUES (1, 1, 1), (2, 1, 1);
            INSERT INTO ties VALUES (1);
            INSERT INTO matches VALUES (1);
            INSERT INTO match_predictions VALUES (1);
            INSERT INTO tie_predictions VALUES (1);
            INSERT INTO match_prediction_scores VALUES (1);
            INSERT INTO tie_prediction_scores VALUES (1);
            """
        )


def _counts(database_path: Path) -> dict[str, int]:
    with sqlite3.connect(database_path) as connection:
        return {
            table_name: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
            )
            for table_name in PRESERVED_TABLES
        }


def _columns(database_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[1])
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
        }


def test_migration_is_idempotent_and_does_not_backfill_historical_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_database(database_path)
    counts_before = _counts(database_path)

    migrate_database(database_path)
    migrate_database(database_path)

    assert _counts(database_path) == counts_before
    assert "stage_key" in _columns(database_path, "stages")
    assert {"round_key", "bracket_position"}.issubset(
        _columns(database_path, "shared_two_legged_ties")
    )
    assert {"round_key", "bracket_position"}.issubset(
        _columns(database_path, "shared_matches")
    )
    assert "version" in _columns(database_path, "shared_fixture_imports")
    assert "sync_generation" in _columns(
        database_path, "shared_tournament_external_sources"
    )
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute("SELECT stage_key FROM stages WHERE id = 1").fetchone()[
                0
            ]
            is None
        )
        assert connection.execute(
            "SELECT round_key, bracket_position FROM shared_two_legged_ties WHERE id = 1"
        ).fetchone() == (None, None)
        assert connection.execute(
            "SELECT round_key, bracket_position FROM shared_matches WHERE id = 1"
        ).fetchone() == (None, None)
        assert (
            connection.execute("SELECT COUNT(*) FROM shared_bracket_nodes").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shared_fixture_imports"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        stage_index = next(
            row
            for row in connection.execute("PRAGMA index_list('stages')")
            if row[1] == "idx_stages_competition_stage_key"
        )
        assert stage_index[2] == 1
        assert stage_index[4] == 1


def test_migration_rolls_back_if_a_preexisting_ledger_has_wrong_shape(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "wrong-shape.db"
    _create_legacy_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE shared_fixture_imports (id INTEGER PRIMARY KEY)"
        )

    with pytest.raises(RuntimeError, match="columns missing"):
        migrate_database(database_path)

    assert "stage_key" not in _columns(database_path, "stages")
    assert "round_key" not in _columns(database_path, "shared_matches")
    assert _counts(database_path)["matches"] == 1


def test_migration_adds_generation_to_preexisting_source_config_idempotently(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pre-generation-source.db"
    _create_legacy_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE shared_tournament_external_sources (
                shared_tournament_id INTEGER NOT NULL
                    REFERENCES shared_tournaments(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                external_event_id TEXT NOT NULL,
                sync_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK (sync_enabled IN (0, 1)),
                enabled_at TEXT,
                last_attempt_at TEXT,
                last_success_at TEXT,
                last_error TEXT,
                version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (shared_tournament_id, source)
            );
            INSERT INTO shared_tournament_external_sources (
                shared_tournament_id, source, external_event_id,
                sync_enabled, enabled_at, last_attempt_at, version
            ) VALUES (
                1, 'football-data.org', 'CL:2026', 1,
                '2026-09-02T12:00:00Z', '2026-09-02T12:05:00Z', 7
            );
            """
        )

    migrate_database(database_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT external_event_id, sync_enabled, enabled_at,
                   last_attempt_at, version, sync_generation
            FROM shared_tournament_external_sources
            WHERE shared_tournament_id = 1 AND source = 'football-data.org'
            """
        ).fetchone()
        assert row == (
            "CL:2026",
            1,
            "2026-09-02T12:00:00Z",
            "2026-09-02T12:05:00Z",
            7,
            1,
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_rejects_ledger_without_durable_identity_constraints(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "missing-identity.db"
    _create_legacy_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE shared_tie_external_links (
                id INTEGER PRIMARY KEY,
                shared_tournament_id INTEGER NOT NULL
                    REFERENCES shared_tournaments(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                external_event_id TEXT NOT NULL,
                external_tie_id TEXT NOT NULL,
                shared_tie_id INTEGER
                    REFERENCES shared_two_legged_ties(id) ON DELETE SET NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                tombstoned_at TEXT
            );
            """
        )

    with pytest.raises(RuntimeError):
        migrate_database(database_path)

    assert "stage_key" not in _columns(database_path, "stages")
    assert _counts(database_path)["matches"] == 1


def test_migrated_trigger_tombstones_unbound_rows_by_bracket_position(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fallback-trigger.db"
    _create_legacy_database(database_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE shared_two_legged_ties
            SET round_key = 'playoff', bracket_position = 1
            WHERE id = 1
            """
        )
        node_id = int(
            connection.execute(
                """
                INSERT INTO shared_bracket_nodes (
                    shared_tournament_id, round_key, bracket_position, node_format
                ) VALUES (1, 'playoff', 1, 'two_legged')
                """
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO shared_tournament_external_sources (
                shared_tournament_id, source, external_event_id, sync_enabled
            ) VALUES (1, 'provider', 'event', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO shared_tie_external_links (
                shared_tournament_id, source, external_event_id, external_tie_id,
                materialization_claim, claim_started_at
            ) VALUES (
                1, 'provider', 'event', 'tie-1',
                'claim-1', '2030-01-01T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO shared_fixture_imports (
                shared_tournament_id, source, external_event_id,
                external_fixture_id, external_tie_id, round_key,
                bracket_position, leg_number, shared_bracket_node_id,
                payload_hash
            ) VALUES (
                1, 'provider', 'event', 'fixture-1', 'tie-1',
                'playoff', 1, 1, ?, 'hash'
            )
            """,
            (node_id,),
        )

        connection.execute("DELETE FROM shared_two_legged_ties WHERE id = 1")
        fixture = connection.execute(
            """
            SELECT import_status, tombstoned_at, shared_tie_id, shared_match_id
            FROM shared_fixture_imports WHERE external_fixture_id = 'fixture-1'
            """
        ).fetchone()
        tie_link = connection.execute(
            """
            SELECT tombstoned_at, shared_tie_id,
                   materialization_claim, claim_started_at
            FROM shared_tie_external_links WHERE external_tie_id = 'tie-1'
            """
        ).fetchone()
        node = connection.execute(
            """
            SELECT sync_status, sync_error, materialized_shared_tie_id
            FROM shared_bracket_nodes WHERE id = ?
            """,
            (node_id,),
        ).fetchone()

        assert fixture[0] == "tombstoned"
        assert fixture[1] is not None
        assert fixture[2:] == (None, None)
        assert tie_link[0] is not None
        assert tie_link[1:] == (None, None, None)
        assert node[0] == "conflict"
        assert "удалено" in node[1]
        assert node[2] is None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_backup_database_preserves_legacy_data_before_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    backup_path = tmp_path / "backup" / "legacy.db.bak"
    _create_legacy_database(database_path)

    backup_database(database_path, backup_path)
    migrate_database(database_path)

    assert backup_path.is_file()
    assert "stage_key" not in _columns(backup_path, "stages")
    assert _counts(backup_path) == {
        table_name: count for table_name, count in _counts(database_path).items()
    }
    with pytest.raises(RuntimeError, match="already exists"):
        backup_database(database_path, backup_path)
