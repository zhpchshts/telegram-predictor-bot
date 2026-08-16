from __future__ import annotations

from pathlib import Path
import sqlite3

from app.database import initialize_database
from scripts.migrate_leaderboard_publication_schema import migrate_database


def test_migration_preserves_publication_messages_and_adds_snapshot_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        chat_id = connection.execute(
            "INSERT INTO chats (telegram_chat_id, title) VALUES (-1001, 'Чат')"
        ).lastrowid
        contest_id = connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug)
            VALUES (?, 'Конкурс', 'contest')
            """,
            (chat_id,),
        ).lastrowid
        publication_id = connection.execute(
            """
            INSERT INTO contest_publications (
                contest_id, publication_type, entity_id,
                desired_revision, settled_revision, desired_action,
                delivery_status, first_event_id, latest_event_id,
                created_at, updated_at
            )
            VALUES (?, 'match_result', 7, 1, 1, 'publish',
                    'published', 1, 1, '2026-01-01', '2026-01-01')
            """,
            (contest_id,),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO contest_publication_messages (
                publication_id, part_number, telegram_message_id,
                content_hash, content_text, sent_at, updated_at
            )
            VALUES (?, 0, 99, 'hash', 'text', '2026-01-01', '2026-01-01')
            """,
            (publication_id,),
        )
        connection.commit()
    finally:
        connection.close()

    _remove_leaderboard_publication_type(database_path)
    migrate_database(database_path)
    migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        table_sql = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'contest_publications'
            """
        ).fetchone()[0]
        snapshot_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'leaderboard_publication_snapshots'
            """
        ).fetchone()
        stored = connection.execute(
            """
            SELECT publication.publication_type, message.telegram_message_id
            FROM contest_publications AS publication
            JOIN contest_publication_messages AS message
                ON message.publication_id = publication.id
            """
        ).fetchone()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert "'leaderboard_snapshot'" in str(table_sql)
    assert snapshot_table is not None
    assert stored == ("match_result", 99)
    assert foreign_key_errors == []


def _remove_leaderboard_publication_type(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    try:
        current_sql = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'contest_publications'
            """
        ).fetchone()[0]
        legacy_sql = str(current_sql).replace(
            "            'leaderboard_snapshot',\n",
            "",
        )
        legacy_sql = legacy_sql.replace(
            "CREATE TABLE contest_publications",
            "CREATE TABLE contest_publications_legacy",
            1,
        )
        connection.execute("DROP TABLE leaderboard_publication_snapshots")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(legacy_sql)
        columns = tuple(
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(contest_publications)"
            ).fetchall()
        )
        column_list = ", ".join(columns)
        connection.execute(
            f"""
            INSERT INTO contest_publications_legacy ({column_list})
            SELECT {column_list} FROM contest_publications
            """
        )
        connection.execute("DROP TABLE contest_publications")
        connection.execute(
            "ALTER TABLE contest_publications_legacy RENAME TO contest_publications"
        )
        connection.commit()
    finally:
        connection.close()
