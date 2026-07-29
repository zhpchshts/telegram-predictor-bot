from __future__ import annotations

from pathlib import Path

from app.database import create_connection, initialize_database


def test_initialize_database_creates_current_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"

    initialize_database(database_path)

    expected_tables = {
        "audit_events",
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

    assert tables_count == 30


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
