from __future__ import annotations

from pathlib import Path

from app.database import create_connection, initialize_database


def test_initialize_database_creates_core_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"

    initialize_database(database_path)

    expected_tables = {
        "chats",
        "users",
        "contests",
        "competitions",
        "scoring_rule_sets",
        "stages",
        "teams",
        "ties",
        "matches",
        "match_predictions",
        "tie_predictions",
        "match_prediction_scores",
        "tie_prediction_scores",
        "event_log",
    }

    with create_connection(database_path) as connection:
        actual_tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table';"
            )
        }
        foreign_keys_enabled = connection.execute("PRAGMA foreign_keys;").fetchone()[0]

    assert expected_tables <= actual_tables
    assert foreign_keys_enabled == 1


def test_initialize_database_allows_multiple_active_contests_in_one_chat(
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

        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (chat_id, "Конкурс ЧМ-2026", "world-cup-2026"),
        )
        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (chat_id, "Конкурс ЛЧ-2026/27", "champions-league-2026-27"),
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
