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
        "contest_admins",
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
