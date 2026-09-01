from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import create_connection, initialize_database


def test_initialize_database_creates_current_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"

    initialize_database(database_path)

    expected_tables = {
        "audit_events",
        "champion_predictions",
        "chat_settings",
        "chats",
        "competitions",
        "contest_creation_requests",
        "contest_publication_messages",
        "contest_publications",
        "contest_teams",
        "contests",
        "event_log",
        "leaderboard_publication_snapshots",
        "match_creation_requests",
        "match_prediction_publication_messages",
        "match_prediction_publications",
        "match_prediction_scores",
        "match_predictions",
        "matches",
        "scoring_rule_sets",
        "stages",
        "supermoderator_assignments",
        "swiss_stage_prediction_selections",
        "swiss_stage_prediction_settings",
        "swiss_stage_predictions",
        "swiss_stage_result_selections",
        "swiss_stage_results",
        "teams",
        "telegram_chat_migrations",
        "tie_prediction_scores",
        "tie_predictions",
        "ties",
        "users",
        "shared_tournaments",
        "shared_tournament_settings",
        "shared_tournament_teams",
        "shared_swiss_stage_result_selections",
        "shared_matches",
        "shared_two_legged_ties",
        "shared_tie_links",
        "shared_match_external_links",
        "shared_bracket_nodes",
        "shared_tournament_external_sources",
        "shared_team_external_links",
        "shared_tie_external_links",
        "shared_fixture_imports",
        "contest_shared_tournaments",
        "shared_match_links",
        "shared_tournament_events",
    }

    with create_connection(database_path) as connection:
        actual_tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        foreign_keys_enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert actual_tables == expected_tables
    assert foreign_keys_enabled == 1
    assert foreign_key_violations == []


def test_initialize_database_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"

    initialize_database(database_path)
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        tables_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchone()[0]

    assert tables_count == 48


def test_initialize_database_rolls_back_all_schema_changes_on_failure(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "historical.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE contests (
                id INTEGER PRIMARY KEY,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX idx_contests_active_chat_id
                ON contests(is_active);
            """
        )

    with pytest.raises(sqlite3.OperationalError, match="no such column: chat_id"):
        initialize_database(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    assert tables == {"contests"}
    assert "idx_contests_active_chat_id" in indexes


def test_create_connection_configures_integrity_and_lock_waiting(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"

    with create_connection(database_path) as connection:
        foreign_keys_enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout_ms = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert foreign_keys_enabled == 1
    assert busy_timeout_ms == 5000


def test_current_schema_supports_only_known_contest_templates(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        chat_id = connection.execute(
            """
            INSERT INTO chats (telegram_chat_id, title)
            VALUES (?, ?)
            """,
            (-1001234567890, "Test chat"),
        ).lastrowid
        football_contest_id = connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug)
            VALUES (?, ?, ?)
            """,
            (chat_id, "World Cup 2026", "world-cup-2026"),
        ).lastrowid
        dota_contest_id = connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, template_key)
            VALUES (?, ?, ?, ?)
            """,
            (
                chat_id,
                "The International 2026",
                "the-international-2026",
                "the_international_2026",
            ),
        ).lastrowid
        champions_league_contest_id = connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, template_key)
            VALUES (?, ?, ?, ?)
            """,
            (
                chat_id,
                "Champions League 2026/27",
                "champions-league-2026-27",
                "champions_league_2026_27",
            ),
        ).lastrowid

        templates = connection.execute(
            """
            SELECT id, template_key
            FROM contests
            ORDER BY id
            """
        ).fetchall()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO contests (chat_id, name, slug, template_key)
                VALUES (?, ?, ?, ?)
                """,
                (chat_id, "Unknown", "unknown", "unknown_template"),
            )

    assert [(row["id"], row["template_key"]) for row in templates] == [
        (football_contest_id, "world_cup_2026"),
        (dota_contest_id, "the_international_2026"),
        (champions_league_contest_id, "champions_league_2026_27"),
    ]


def test_current_schema_supports_champions_league_shared_tournament_template(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        shared_tournament_id = connection.execute(
            """
            INSERT INTO shared_tournaments (
                name,
                template_key,
                created_by_telegram_user_id
            )
            VALUES (?, ?, ?)
            """,
            (
                "Лига чемпионов 2026/27",
                "champions_league_2026_27",
                123,
            ),
        ).lastrowid

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO shared_tournaments (
                    name,
                    template_key,
                    created_by_telegram_user_id
                )
                VALUES (?, ?, ?)
                """,
                ("Unknown", "unknown_template", 123),
            )

    assert shared_tournament_id == 1


def test_current_schema_allows_only_supported_best_of_values(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        chat_id = connection.execute(
            "INSERT INTO chats (telegram_chat_id, title) VALUES (?, ?)",
            (-1001234567890, "Test chat"),
        ).lastrowid
        contest_id = connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug)
            VALUES (?, ?, ?)
            """,
            (chat_id, "World Cup 2026", "world-cup-2026"),
        ).lastrowid
        competition_id = connection.execute(
            """
            INSERT INTO competitions (
                contest_id,
                name,
                season,
                competition_type
            )
            VALUES (?, ?, ?, ?)
            """,
            (contest_id, "World Cup", "2026", "world_cup"),
        ).lastrowid
        scoring_rule_set_id = connection.execute(
            """
            INSERT INTO scoring_rule_sets (competition_id, version)
            VALUES (?, 1)
            """,
            (competition_id,),
        ).lastrowid
        stage_id = connection.execute(
            """
            INSERT INTO stages (
                competition_id,
                name,
                position,
                stage_type
            )
            VALUES (?, ?, 0, ?)
            """,
            (competition_id, "Playoffs", "knockout"),
        ).lastrowid
        home_team_id = connection.execute(
            "INSERT INTO teams (name) VALUES (?)",
            ("Home",),
        ).lastrowid
        away_team_id = connection.execute(
            "INSERT INTO teams (name) VALUES (?)",
            ("Away",),
        ).lastrowid

        for position, best_of in enumerate((None, 3, 5)):
            connection.execute(
                """
                INSERT INTO matches (
                    stage_id,
                    scoring_rule_set_id,
                    home_team_id,
                    away_team_id,
                    starts_at_utc,
                    best_of
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    stage_id,
                    scoring_rule_set_id,
                    home_team_id,
                    away_team_id,
                    f"2026-08-0{position + 1}T12:00:00Z",
                    best_of,
                ),
            )

        stored_values = connection.execute(
            "SELECT best_of FROM matches ORDER BY id"
        ).fetchall()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO matches (
                    stage_id,
                    scoring_rule_set_id,
                    home_team_id,
                    away_team_id,
                    starts_at_utc,
                    best_of
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    stage_id,
                    scoring_rule_set_id,
                    home_team_id,
                    away_team_id,
                    "2026-08-10T12:00:00Z",
                    1,
                ),
            )

    assert [row["best_of"] for row in stored_values] == [None, 3, 5]


def test_current_schema_allows_multiple_active_contests_in_one_chat(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        chat_id = connection.execute(
            """
            INSERT INTO chats (telegram_chat_id, title)
            VALUES (?, ?)
            """,
            (-1001234567890, "Тестовый чат"),
        ).lastrowid
        connection.executemany(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, 1)
            """,
            [
                (chat_id, "Конкурс ЧМ-2026", "world-cup-2026"),
                (
                    chat_id,
                    "Конкурс ЛЧ-2026/27",
                    "champions-league-2026-27",
                ),
            ],
        )
        active_contests_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM contests
            WHERE chat_id = ? AND is_active = 1
            """,
            (chat_id,),
        ).fetchone()[0]

    assert active_contests_count == 2
