from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from app.database import initialize_database
from scripts.migrate_champions_league_2026_27_schema import migrate_database


HISTORICAL_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE chats (
    id INTEGER PRIMARY KEY,
    telegram_chat_id INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE teams (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE contests (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    template_key TEXT NOT NULL DEFAULT 'world_cup_2026' CHECK (
        template_key IN ('world_cup_2026', 'the_international_2026')
    ),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    champion_prediction_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (champion_prediction_enabled IN (0, 1)),
    champion_prediction_deadline_at TEXT,
    champion_prediction_points INTEGER NOT NULL DEFAULT 5
        CHECK (champion_prediction_points >= 0),
    champion_team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    match_prediction_publication_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (match_prediction_publication_enabled IN (0, 1)),
    match_prediction_publication_enabled_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_contests_active_name
ON contests(name)
WHERE is_active = 1;

CREATE TABLE shared_tournaments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    template_key TEXT NOT NULL CHECK (
        template_key IN ('world_cup_2026', 'the_international_2026')
    ),
    is_archived INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by_telegram_user_id INTEGER NOT NULL CHECK (
        created_by_telegram_user_id > 0
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX idx_shared_tournaments_active_name
ON shared_tournaments(lower(name))
WHERE is_archived = 0;

CREATE TABLE contest_shared_tournaments (
    contest_id INTEGER PRIMARY KEY
        REFERENCES contests(id) ON DELETE CASCADE,
    shared_tournament_id INTEGER NOT NULL
        REFERENCES shared_tournaments(id),
    linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (shared_tournament_id, contest_id)
);

CREATE TABLE shared_tournament_settings (
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

CREATE TABLE migration_audit (
    table_name TEXT NOT NULL,
    entity_id INTEGER NOT NULL
);

CREATE TRIGGER contests_insert_audit
AFTER INSERT ON contests
BEGIN
    INSERT INTO migration_audit (table_name, entity_id)
    VALUES ('contests', NEW.id);
END;

CREATE TRIGGER shared_tournaments_insert_audit
AFTER INSERT ON shared_tournaments
BEGIN
    INSERT INTO migration_audit (table_name, entity_id)
    VALUES ('shared_tournaments', NEW.id);
END;
"""


def _create_historical_database(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(HISTORICAL_SCHEMA)
        connection.executescript(
            """
            INSERT INTO chats (id, telegram_chat_id, title)
            VALUES (1, -1001, 'Predictions');
            INSERT INTO teams (id, name)
            VALUES (1, 'Existing champion');
            INSERT INTO contests (
                id, chat_id, name, slug, template_key,
                champion_prediction_enabled,
                champion_prediction_deadline_at,
                champion_prediction_points,
                champion_team_id,
                match_prediction_publication_enabled,
                match_prediction_publication_enabled_at,
                created_at
            ) VALUES (
                10, 1, 'Historical contest', 'historical-contest',
                'world_cup_2026', 1, '2026-06-01T12:00:00Z', 7, 1, 1,
                '2026-05-01T12:00:00Z', '2026-04-01T12:00:00Z'
            );
            INSERT INTO shared_tournaments (
                id, name, template_key, version,
                created_by_telegram_user_id, created_at, updated_at
            ) VALUES (
                20, 'Historical shared tournament', 'world_cup_2026', 4,
                123, '2026-04-01T12:00:00Z', '2026-05-01T12:00:00Z'
            );
            INSERT INTO contest_shared_tournaments (
                contest_id, shared_tournament_id, linked_at
            ) VALUES (10, 20, '2026-05-02T12:00:00Z');
            INSERT INTO shared_tournament_settings (
                shared_tournament_id,
                swiss_stage_prediction_enabled,
                swiss_stage_prediction_deadline_at,
                swiss_direct_qualifier_count,
                swiss_elimination_qualifier_count
            ) VALUES (20, 1, '2026-08-28T16:00:00Z', 8, 16);
            DELETE FROM migration_audit;
            """
        )
    finally:
        connection.close()


def test_migration_preserves_data_dependencies_and_schema_objects(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    _create_historical_database(database_path)

    migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        contest = connection.execute(
            """
            SELECT id, chat_id, name, slug, template_key, is_active,
                   champion_prediction_enabled,
                   champion_prediction_deadline_at,
                   champion_prediction_points, champion_team_id,
                   match_prediction_publication_enabled,
                   match_prediction_publication_enabled_at, created_at
            FROM contests
            """
        ).fetchone()
        tournament = connection.execute(
            """
            SELECT id, name, template_key, is_archived, version,
                   created_by_telegram_user_id, created_at, updated_at
            FROM shared_tournaments
            """
        ).fetchone()
        assert contest == (
            10,
            1,
            "Historical contest",
            "historical-contest",
            "world_cup_2026",
            1,
            1,
            "2026-06-01T12:00:00Z",
            7,
            1,
            1,
            "2026-05-01T12:00:00Z",
            "2026-04-01T12:00:00Z",
        )
        assert tournament == (
            20,
            "Historical shared tournament",
            "world_cup_2026",
            0,
            4,
            123,
            "2026-04-01T12:00:00Z",
            "2026-05-01T12:00:00Z",
        )
        assert connection.execute(
            "SELECT contest_id, shared_tournament_id FROM contest_shared_tournaments"
        ).fetchone() == (10, 20)
        assert connection.execute(
            """
            SELECT swiss_stage_prediction_enabled,
                   swiss_stage_prediction_deadline_at,
                   swiss_direct_qualifier_count,
                   swiss_elimination_qualifier_count
            FROM shared_tournament_settings
            """
        ).fetchone() == (1, "2026-08-28T16:00:00Z", 8, 16)

        schema_objects = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE name IN (
                    'idx_contests_active_name',
                    'idx_shared_tournaments_active_name',
                    'contests_insert_audit',
                    'shared_tournaments_insert_audit'
                )
                """
            )
        }
        assert schema_objects == {
            "idx_contests_active_name",
            "idx_shared_tournaments_active_name",
            "contests_insert_audit",
            "shared_tournaments_insert_audit",
        }

        connection.execute(
            """
            INSERT INTO contests (
                id, chat_id, name, slug, template_key
            ) VALUES (11, 1, 'Champions League contest', 'champions-league', ?)
            """,
            ("champions_league_2026_27",),
        )
        connection.execute(
            """
            INSERT INTO shared_tournaments (
                id, name, template_key, created_by_telegram_user_id
            ) VALUES (21, 'Champions League 2026/27', ?, 123)
            """,
            ("champions_league_2026_27",),
        )
        connection.execute(
            """
            INSERT INTO contest_shared_tournaments (
                contest_id, shared_tournament_id
            ) VALUES (11, 21)
            """
        )
        connection.commit()
    finally:
        connection.close()

    # This intentionally reduced historical fixture is not a full production
    # schema. Once UCL data exists, a repeat must fail closed instead of
    # silently skipping the missing prediction settings tables.
    with pytest.raises(RuntimeError, match="Incomplete schema for Champions League"):
        migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM contests").fetchone()[0] == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM shared_tournaments").fetchone()[0]
            == 2
        )
        assert connection.execute(
            "SELECT table_name, entity_id FROM migration_audit ORDER BY table_name"
        ).fetchall() == [("contests", 11), ("shared_tournaments", 21)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO contests (
                    id, chat_id, name, slug, template_key
                ) VALUES (12, 1, 'Invalid', 'invalid', 'unsupported')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO shared_tournaments (
                    id, name, template_key, created_by_telegram_user_id
                ) VALUES (22, 'Invalid', 'unsupported', 123)
                """
            )
    finally:
        connection.close()


def test_migration_refuses_unexpected_schema_without_partial_rebuild(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    _create_historical_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("ALTER TABLE shared_tournaments ADD COLUMN surprise TEXT")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="Unexpected shared_tournaments schema"):
        migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        contest_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'contests'"
            ).fetchone()[0]
        )
        shared_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'shared_tournaments'"
            ).fetchone()[0]
        )
        assert "champions_league_2026_27" not in contest_sql
        assert "champions_league_2026_27" not in shared_sql
        assert connection.execute("SELECT COUNT(*) FROM contests").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM shared_tournaments").fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """
            SELECT name FROM sqlite_master
            WHERE name LIKE '%__champions_league_2026_27_new'
            """
            ).fetchall()
            == []
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_migration_is_a_noop_for_current_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "current.db"
    initialize_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            INSERT INTO chats (id, telegram_chat_id, title)
            VALUES (1, -1001, 'Champions League predictions');
            INSERT INTO contests (
                id, chat_id, name, slug, template_key
            ) VALUES (
                10, 1, 'Champions League', 'champions-league',
                'champions_league_2026_27'
            );
            INSERT INTO swiss_stage_prediction_settings (
                contest_id, direct_qualifier_count, elimination_qualifier_count
            ) VALUES (10, 8, 12);
            INSERT INTO shared_tournaments (
                id, name, template_key, created_by_telegram_user_id
            ) VALUES (
                20, 'Shared Champions League', 'champions_league_2026_27', 123
            );
            INSERT INTO shared_tournament_settings (
                shared_tournament_id,
                swiss_direct_qualifier_count,
                swiss_elimination_qualifier_count
            ) VALUES (20, 8, 12);
            INSERT INTO contest_shared_tournaments (
                contest_id, shared_tournament_id
            ) VALUES (10, 20);
            """
        )
        connection.commit()
    finally:
        connection.close()

    migrate_database(database_path)
    migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        for table_name in ("contests", "shared_tournaments"):
            table_sql = str(
                connection.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table' AND name = ?
                    """,
                    (table_name,),
                ).fetchone()[0]
            )
            assert "champions_league_2026_27" in table_sql
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute(
            """
            SELECT direct_qualifier_count, elimination_qualifier_count
            FROM swiss_stage_prediction_settings
            WHERE contest_id = 10
            """
        ).fetchone() == (8, 12)
    finally:
        connection.close()


def test_migration_normalizes_unlocked_legacy_ucl_limits(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-ucl.db"
    initialize_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            INSERT INTO chats (id, telegram_chat_id, title)
            VALUES (1, -1001, 'Champions League predictions');
            INSERT INTO contests (
                id, chat_id, name, slug, template_key
            ) VALUES (
                10, 1, 'Champions League', 'champions-league',
                'champions_league_2026_27'
            );
            INSERT INTO swiss_stage_prediction_settings (
                contest_id, direct_qualifier_count, elimination_qualifier_count
            ) VALUES (10, 8, 16);
            INSERT INTO shared_tournaments (
                id, name, template_key, created_by_telegram_user_id
            ) VALUES (
                20, 'Shared Champions League', 'champions_league_2026_27', 123
            );
            INSERT INTO shared_tournament_settings (
                shared_tournament_id,
                swiss_direct_qualifier_count,
                swiss_elimination_qualifier_count
            ) VALUES (20, 8, 16);
            INSERT INTO contest_shared_tournaments (
                contest_id, shared_tournament_id
            ) VALUES (10, 20);
            """
        )
        connection.commit()
    finally:
        connection.close()

    migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            """
            SELECT direct_qualifier_count, elimination_qualifier_count
            FROM swiss_stage_prediction_settings
            WHERE contest_id = 10
            """
        ).fetchone() == (8, 12)
        assert connection.execute(
            """
            SELECT swiss_direct_qualifier_count,
                   swiss_elimination_qualifier_count
            FROM shared_tournament_settings
            WHERE shared_tournament_id = 20
            """
        ).fetchone() == (8, 12)
    finally:
        connection.close()


def test_migration_refuses_legacy_ucl_limits_with_prediction(tmp_path: Path) -> None:
    database_path = tmp_path / "locked-ucl.db"
    initialize_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            INSERT INTO chats (id, telegram_chat_id, title)
            VALUES (1, -1001, 'Champions League predictions');
            INSERT INTO contests (
                id, chat_id, name, slug, template_key
            ) VALUES (
                10, 1, 'Champions League', 'champions-league',
                'champions_league_2026_27'
            );
            INSERT INTO swiss_stage_prediction_settings (
                contest_id, direct_qualifier_count, elimination_qualifier_count
            ) VALUES (10, 8, 16);
            INSERT INTO contests (
                id, chat_id, name, slug, template_key
            ) VALUES (
                11, 1, 'Locked Champions League', 'locked-champions-league',
                'champions_league_2026_27'
            );
            INSERT INTO swiss_stage_prediction_settings (
                contest_id, direct_qualifier_count, elimination_qualifier_count
            ) VALUES (11, 8, 16);
            INSERT INTO users (id, telegram_user_id, first_name)
            VALUES (1, 123, 'Participant');
            INSERT INTO swiss_stage_predictions (id, contest_id, user_id)
            VALUES (100, 11, 1);
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="already has predictions or a result"):
        migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            """
            SELECT direct_qualifier_count, elimination_qualifier_count
            FROM swiss_stage_prediction_settings
            WHERE contest_id IN (10, 11)
            ORDER BY contest_id
            """
        ).fetchall() == [(8, 16), (8, 16)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_migration_refuses_ucl_contest_without_settings(tmp_path: Path) -> None:
    database_path = tmp_path / "missing-settings.db"
    initialize_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            INSERT INTO chats (id, telegram_chat_id, title)
            VALUES (1, -1001, 'Champions League predictions');
            INSERT INTO contests (
                id, chat_id, name, slug, template_key
            ) VALUES (
                10, 1, 'Champions League', 'champions-league',
                'champions_league_2026_27'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="contest 10 is missing Swiss settings"):
        migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM contests WHERE id = 10"
        ).fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_migration_refuses_legacy_shared_limits_with_linked_result(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "locked-shared-ucl.db"
    initialize_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            INSERT INTO chats (id, telegram_chat_id, title)
            VALUES (1, -1001, 'Champions League predictions');
            INSERT INTO contests (
                id, chat_id, name, slug, template_key
            ) VALUES (
                10, 1, 'Champions League', 'champions-league',
                'champions_league_2026_27'
            );
            INSERT INTO swiss_stage_prediction_settings (
                contest_id, direct_qualifier_count, elimination_qualifier_count
            ) VALUES (10, 8, 12);
            INSERT INTO swiss_stage_results (contest_id) VALUES (10);
            INSERT INTO shared_tournaments (
                id, name, template_key, created_by_telegram_user_id
            ) VALUES (
                20, 'Shared Champions League', 'champions_league_2026_27', 123
            );
            INSERT INTO shared_tournament_settings (
                shared_tournament_id,
                swiss_direct_qualifier_count,
                swiss_elimination_qualifier_count
            ) VALUES (20, 8, 16);
            INSERT INTO contest_shared_tournaments (
                contest_id, shared_tournament_id
            ) VALUES (10, 20);
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="shared tournament 20.*already has"):
        migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            """
            SELECT swiss_direct_qualifier_count,
                   swiss_elimination_qualifier_count
            FROM shared_tournament_settings
            WHERE shared_tournament_id = 20
            """
        ).fetchone() == (8, 16)
        assert connection.execute(
            """
            SELECT direct_qualifier_count, elimination_qualifier_count
            FROM swiss_stage_prediction_settings
            WHERE contest_id = 10
            """
        ).fetchone() == (8, 12)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_migration_refuses_unsupported_ucl_limits(tmp_path: Path) -> None:
    database_path = tmp_path / "unsupported-ucl.db"
    initialize_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            INSERT INTO chats (id, telegram_chat_id, title)
            VALUES (1, -1001, 'Champions League predictions');
            INSERT INTO contests (
                id, chat_id, name, slug, template_key
            ) VALUES (
                10, 1, 'Champions League', 'champions-league',
                'champions_league_2026_27'
            );
            INSERT INTO swiss_stage_prediction_settings (
                contest_id, direct_qualifier_count, elimination_qualifier_count
            ) VALUES (10, 8, 15);
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match=r"unsupported limits 8\+15"):
        migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            """
            SELECT elimination_qualifier_count
            FROM swiss_stage_prediction_settings
            WHERE contest_id = 10
            """
        ).fetchone() == (15,)
    finally:
        connection.close()
