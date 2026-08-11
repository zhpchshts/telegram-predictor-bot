from __future__ import annotations

from pathlib import Path

import pytest

from app.chat_migration_service import (
    TelegramChatMigrationError,
    is_telegram_chat_migrated,
    migrate_telegram_chat,
)
from app.database import create_connection, initialize_database


OLD_TELEGRAM_CHAT_ID = -5456940219
NEW_TELEGRAM_CHAT_ID = -1003944707033


def _create_chat_data(database_path: Path) -> tuple[int, int, int]:
    with create_connection(database_path) as connection:
        chat_id = int(
            connection.execute(
                """
                INSERT INTO chats (telegram_chat_id, title)
                VALUES (?, ?)
                """,
                (OLD_TELEGRAM_CHAT_ID, "Old title"),
            ).lastrowid
        )
        user_id = int(
            connection.execute(
                """
                INSERT INTO users (telegram_user_id, first_name)
                VALUES (?, ?)
                """,
                (123, "Eugene"),
            ).lastrowid
        )
        contest_id = int(
            connection.execute(
                """
                INSERT INTO contests (chat_id, name, slug)
                VALUES (?, ?, ?)
                """,
                (chat_id, "The International 2026", "ti-2026"),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO supermoderator_assignments (
                chat_id,
                user_id,
                assigned_by_user_id
            )
            VALUES (?, ?, ?)
            """,
            (chat_id, user_id, user_id),
        )
        connection.execute(
            """
            INSERT INTO chat_settings (chat_id, app_button_text)
            VALUES (?, ?)
            """,
            (chat_id, "Открыть прогнозы"),
        )
        connection.execute(
            """
            INSERT INTO audit_events (
                created_at,
                chat_id,
                actor_user_id,
                actor_role,
                event_type,
                entity_type,
                entity_id,
                contest_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-08-09T15:40:55Z",
                OLD_TELEGRAM_CHAT_ID,
                123,
                "participant",
                "contest_created",
                "contest",
                contest_id,
                contest_id,
            ),
        )
    return chat_id, user_id, contest_id


def test_migrate_telegram_chat_preserves_chat_scoped_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    chat_id, user_id, contest_id = _create_chat_data(database_path)

    result = migrate_telegram_chat(
        database_path=database_path,
        old_telegram_chat_id=OLD_TELEGRAM_CHAT_ID,
        new_telegram_chat_id=NEW_TELEGRAM_CHAT_ID,
        new_chat_title="New title",
    )

    assert result.chat_id == chat_id
    assert result.old_telegram_chat_id == OLD_TELEGRAM_CHAT_ID
    assert result.new_telegram_chat_id == NEW_TELEGRAM_CHAT_ID
    assert result.migrated_audit_event_count == 1
    assert result.already_migrated is False
    assert is_telegram_chat_migrated(
        database_path=database_path,
        telegram_chat_id=OLD_TELEGRAM_CHAT_ID,
    )

    with create_connection(database_path) as connection:
        chat_row = connection.execute(
            "SELECT id, telegram_chat_id, title FROM chats"
        ).fetchone()
        contest_row = connection.execute(
            "SELECT chat_id FROM contests WHERE id = ?",
            (contest_id,),
        ).fetchone()
        assignment_row = connection.execute(
            """
            SELECT chat_id, user_id
            FROM supermoderator_assignments
            """
        ).fetchone()
        audit_row = connection.execute("SELECT chat_id FROM audit_events").fetchone()
        settings_row = connection.execute(
            "SELECT chat_id, app_button_text FROM chat_settings"
        ).fetchone()
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert dict(chat_row) == {
        "id": chat_id,
        "telegram_chat_id": NEW_TELEGRAM_CHAT_ID,
        "title": "New title",
    }
    assert dict(contest_row) == {"chat_id": chat_id}
    assert dict(assignment_row) == {"chat_id": chat_id, "user_id": user_id}
    assert dict(audit_row) == {"chat_id": NEW_TELEGRAM_CHAT_ID}
    assert dict(settings_row) == {
        "chat_id": chat_id,
        "app_button_text": "Открыть прогнозы",
    }
    assert foreign_key_violations == []


def test_migrate_telegram_chat_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    chat_id, _, _ = _create_chat_data(database_path)

    migrate_telegram_chat(
        database_path=database_path,
        old_telegram_chat_id=OLD_TELEGRAM_CHAT_ID,
        new_telegram_chat_id=NEW_TELEGRAM_CHAT_ID,
    )
    result = migrate_telegram_chat(
        database_path=database_path,
        old_telegram_chat_id=OLD_TELEGRAM_CHAT_ID,
        new_telegram_chat_id=NEW_TELEGRAM_CHAT_ID,
        new_chat_title="Latest title",
    )

    assert result.chat_id == chat_id
    assert result.migrated_audit_event_count == 0
    assert result.already_migrated is True
    with create_connection(database_path) as connection:
        chat_row = connection.execute(
            "SELECT telegram_chat_id, title FROM chats"
        ).fetchone()
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM telegram_chat_migrations"
        ).fetchone()[0]
    assert dict(chat_row) == {
        "telegram_chat_id": NEW_TELEGRAM_CHAT_ID,
        "title": "Latest title",
    }
    assert migration_count == 1


def test_migrate_telegram_chat_rejects_existing_destination(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    _create_chat_data(database_path)
    with create_connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO chats (telegram_chat_id, title)
            VALUES (?, ?)
            """,
            (NEW_TELEGRAM_CHAT_ID, "Conflicting chat"),
        )

    with pytest.raises(
        TelegramChatMigrationError,
        match="Destination Telegram chat already exists",
    ):
        migrate_telegram_chat(
            database_path=database_path,
            old_telegram_chat_id=OLD_TELEGRAM_CHAT_ID,
            new_telegram_chat_id=NEW_TELEGRAM_CHAT_ID,
        )

    with create_connection(database_path) as connection:
        source_row = connection.execute(
            """
            SELECT id
            FROM chats
            WHERE telegram_chat_id = ?
            """,
            (OLD_TELEGRAM_CHAT_ID,),
        ).fetchone()
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM telegram_chat_migrations"
        ).fetchone()[0]
    assert source_row is not None
    assert migration_count == 0
