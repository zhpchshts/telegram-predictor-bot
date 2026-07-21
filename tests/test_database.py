from __future__ import annotations

from pathlib import Path

from app.database import create_connection, initialize_database


def test_initialize_database_creates_core_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"

    initialize_database(database_path)

    expected_tables = {
        "chats",
        "users",
        "supermoderator_assignments",
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

    with create_connection(database_path) as connection:
        assignment_indexes = {
            row["name"]
            for row in connection.execute(
                "PRAGMA index_list(supermoderator_assignments)"
            )
        }

    assert {
        "idx_supermoderator_assignments_active_chat_user",
        "idx_supermoderator_assignments_chat_user_history",
    } <= assignment_indexes


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

        prediction_id = connection.execute(
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
        ).lastrowid

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

        chat_row = connection.execute(
            """
            SELECT id, telegram_chat_id, title
            FROM chats
            WHERE id = ?
            """,
            (chat_id,),
        ).fetchone()

        user_row = connection.execute(
            """
            SELECT id, telegram_user_id, first_name
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()

        prediction_row = connection.execute(
            """
            SELECT id, match_id, user_id, predicted_home_score, predicted_away_score
            FROM match_predictions
            WHERE id = ?
            """,
            (prediction_id,),
        ).fetchone()

        champion_predictions_table = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'champion_predictions'
            """
        ).fetchone()

        supermoderator_assignments_table = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'supermoderator_assignments'
            """
        ).fetchone()

        supermoderator_assignment_indexes = {
            row["name"]
            for row in connection.execute(
                "PRAGMA index_list(supermoderator_assignments)"
            )
        }

    assert contest_row is not None
    assert contest_row["id"] == contest_id
    assert contest_row["name"] == "Тестовый конкурс"
    assert contest_row["champion_prediction_enabled"] == 0
    assert contest_row["champion_prediction_deadline_at"] is None
    assert contest_row["champion_prediction_points"] == 5
    assert contest_row["champion_team_id"] is None
    assert chat_row is not None
    assert chat_row["id"] == chat_id
    assert chat_row["telegram_chat_id"] == -1001234567890
    assert user_row is not None
    assert user_row["id"] == user_id
    assert user_row["telegram_user_id"] == 123456789
    assert prediction_row is not None
    assert prediction_row["id"] == prediction_id
    assert prediction_row["match_id"] == 987654321
    assert prediction_row["user_id"] == user_id
    assert prediction_row["predicted_home_score"] == 2
    assert prediction_row["predicted_away_score"] == 1
    assert champion_predictions_table is not None
    assert supermoderator_assignments_table is not None
    assert {
        "idx_supermoderator_assignments_active_chat_user",
        "idx_supermoderator_assignments_chat_user_history",
    } <= supermoderator_assignment_indexes
