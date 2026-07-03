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
        "champion_predictions",
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


def test_initialize_database_migrates_existing_contests_without_losing_predictions(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"

    with create_connection(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE chats (
              id INTEGER PRIMARY KEY,
              telegram_chat_id INTEGER NOT NULL UNIQUE,
              title TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE users (
              id INTEGER PRIMARY KEY,
              telegram_user_id INTEGER NOT NULL UNIQUE,
              username TEXT,
              first_name TEXT NOT NULL,
              last_name TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE contests (
              id INTEGER PRIMARY KEY,
              chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              slug TEXT NOT NULL UNIQUE,
              is_active INTEGER NOT NULL DEFAULT 1
                CHECK (is_active IN (0, 1)),
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE match_predictions (
              id INTEGER PRIMARY KEY,
              match_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL,
              predicted_home_score INTEGER NOT NULL,
              predicted_away_score INTEGER NOT NULL,
              submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE (match_id, user_id)
            );
            """
        )

        chat_id = connection.execute(
            """
            INSERT INTO chats (telegram_chat_id, title)
            VALUES (?, ?)
            """,
            (-1001234567890, "Тестовый чат"),
        ).lastrowid

        user_id = connection.execute(
            """
            INSERT INTO users (telegram_user_id, first_name)
            VALUES (?, ?)
            """,
            (123456789, "Евгений"),
        ).lastrowid

        contest_id = connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug)
            VALUES (?, ?, ?)
            """,
            (chat_id, "Тестовый конкурс", "test-contest"),
        ).lastrowid

        connection.execute(
            """
            INSERT INTO match_predictions (
              match_id,
              user_id,
              predicted_home_score,
              predicted_away_score
            )
            VALUES (?, ?, ?, ?)
            """,
            (987654321, user_id, 2, 1),
        )

    initialize_database(database_path)

    with create_connection(database_path) as connection:
        contest_row = connection.execute(
            """
            SELECT
              id,
              name,
              champion_prediction_enabled,
              champion_prediction_deadline_at,
              champion_prediction_points,
              champion_team_id
            FROM contests
            WHERE id = ?
            """,
            (contest_id,),
        ).fetchone()

        prediction_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM match_predictions
            """
        ).fetchone()[0]

        champion_predictions_table = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'champion_predictions'
            """
        ).fetchone()

    assert contest_row is not None
    assert contest_row["id"] == contest_id
    assert contest_row["name"] == "Тестовый конкурс"
    assert contest_row["champion_prediction_enabled"] == 0
    assert contest_row["champion_prediction_deadline_at"] is None
    assert contest_row["champion_prediction_points"] == 5
    assert contest_row["champion_team_id"] is None
    assert prediction_count == 1
    assert champion_predictions_table is not None
