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
        "champion_prediction_candidates",
        "champion_predictions",
        "chats",
        "competitions",
        "contest_creation_requests",
        "contest_publication_messages",
        "contest_publications",
        "contest_teams",
        "contests",
        "event_log",
        "match_creation_requests",
        "match_prediction_publication_messages",
        "match_prediction_publications",
        "match_prediction_scores",
        "match_predictions",
        "matches",
        "scoring_rule_sets",
        "stages",
        "supermoderator_assignments",
        "swiss_stage_prediction_candidates",
        "swiss_stage_prediction_selections",
        "swiss_stage_prediction_settings",
        "swiss_stage_predictions",
        "swiss_stage_result_selections",
        "swiss_stage_results",
        "teams",
        "tie_prediction_scores",
        "tie_predictions",
        "ties",
        "users",
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

    assert tables_count == 31


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
    ]


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


def test_champion_candidate_snapshot_is_ordered_unique_and_contest_scoped(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        chat_id = connection.execute(
            "INSERT INTO chats (telegram_chat_id, title) VALUES (?, ?)",
            (-1001234567890, "Test chat"),
        ).lastrowid
        first_contest_id = connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug)
            VALUES (?, ?, ?)
            """,
            (chat_id, "First", "first"),
        ).lastrowid
        second_contest_id = connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug)
            VALUES (?, ?, ?)
            """,
            (chat_id, "Second", "second"),
        ).lastrowid
        first_team_id = connection.execute(
            "INSERT INTO teams (name) VALUES (?)",
            ("Team A",),
        ).lastrowid
        second_team_id = connection.execute(
            "INSERT INTO teams (name) VALUES (?)",
            ("Team B",),
        ).lastrowid

        connection.executemany(
            """
            INSERT INTO champion_prediction_candidates (
                contest_id,
                team_id,
                position
            )
            VALUES (?, ?, ?)
            """,
            [
                (first_contest_id, first_team_id, 0),
                (first_contest_id, second_team_id, 1),
                (second_contest_id, first_team_id, 0),
            ],
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO champion_prediction_candidates (
                    contest_id,
                    team_id,
                    position
                )
                VALUES (?, ?, ?)
                """,
                (first_contest_id, first_team_id, 2),
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO champion_prediction_candidates (
                    contest_id,
                    team_id,
                    position
                )
                VALUES (?, ?, ?)
                """,
                (first_contest_id, second_team_id, 0),
            )

        connection.execute("DELETE FROM contests WHERE id = ?", (first_contest_id,))
        remaining_rows = connection.execute(
            """
            SELECT contest_id, team_id, position
            FROM champion_prediction_candidates
            ORDER BY contest_id, position
            """
        ).fetchall()

    assert [tuple(row) for row in remaining_rows] == [
        (second_contest_id, first_team_id, 0)
    ]


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
