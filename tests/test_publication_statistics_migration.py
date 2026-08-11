from __future__ import annotations

from pathlib import Path
import sqlite3

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import create_world_cup_2026_contest
from app.database import initialize_database
from scripts.migrate_publication_statistics_schema import migrate_database


def test_migration_preserves_publications_and_expands_allowed_types(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest_id = create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=-1001,
        chat_title="Чат",
        telegram_user_id=101,
        first_name="Администратор",
        last_name=None,
        username="admin",
        contest_name="Конкурс",
        idempotency_key="contest",
        audit_actor=AuditActor(
            telegram_chat_id=-1001,
            telegram_user_id=101,
            role=AuditActorRole.TELEGRAM_ADMIN,
        ),
    ).contest.id
    _downgrade_publication_check(database_path)
    with sqlite3.connect(database_path) as connection:
        publication_id = connection.execute(
            """
            INSERT INTO contest_publications (
                contest_id, publication_type, entity_id,
                desired_revision, settled_revision, desired_action,
                delivery_status, first_event_id, latest_event_id,
                created_at, updated_at
            )
            VALUES (?, 'contest_completed', ?, 1, 1, 'publish',
                    'published', 7, 7, '2026-01-01', '2026-01-01')
            """,
            (contest_id, contest_id),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO contest_publication_messages (
                publication_id, part_number, telegram_message_id,
                content_hash, content_text, sent_at, updated_at
            )
            VALUES (?, 0, 123, 'hash', 'text', '2026-01-01', '2026-01-01')
            """,
            (publication_id,),
        )

    migrate_database(database_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        preserved = connection.execute(
            """
            SELECT publication.publication_type, message.telegram_message_id
            FROM contest_publications AS publication
            JOIN contest_publication_messages AS message
                ON message.publication_id = publication.id
            """
        ).fetchone()
        connection.execute(
            """
            INSERT INTO contest_publications (
                contest_id, publication_type, entity_id,
                desired_revision, settled_revision, desired_action,
                delivery_status, first_event_id, latest_event_id,
                created_at, updated_at
            )
            VALUES (?, 'swiss_predictions', ?, 1, 0, 'publish',
                    'pending', 8, 8, '2026-01-02', '2026-01-02')
            """,
            (contest_id, contest_id),
        )
        connection.execute(
            """
            INSERT INTO contest_publications (
                contest_id, publication_type, entity_id,
                desired_revision, settled_revision, desired_action,
                delivery_status, first_event_id, latest_event_id,
                created_at, updated_at
            )
            VALUES (?, 'swiss_result', ?, 1, 0, 'publish',
                    'pending', 9, 9, '2026-01-03', '2026-01-03')
            """,
            (contest_id, contest_id),
        )
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert dict(preserved) == {
        "publication_type": "contest_completed",
        "telegram_message_id": 123,
    }
    assert foreign_key_errors == []


def _downgrade_publication_check(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    try:
        current_sql = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'contest_publications'
            """
        ).fetchone()[0]
        legacy_sql = str(current_sql).replace(
            "            'swiss_predictions',\n            'swiss_result',\n",
            "",
        )
        legacy_sql = legacy_sql.replace(
            "CREATE TABLE contest_publications",
            "CREATE TABLE contest_publications_legacy",
            1,
        )
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
