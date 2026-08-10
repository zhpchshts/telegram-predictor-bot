from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.database import database_connection


class TelegramChatMigrationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramChatMigrationResult:
    chat_id: int
    old_telegram_chat_id: int
    new_telegram_chat_id: int
    migrated_audit_event_count: int
    already_migrated: bool


def migrate_telegram_chat(
    *,
    database_path: Path,
    old_telegram_chat_id: int,
    new_telegram_chat_id: int,
    new_chat_title: str | None = None,
) -> TelegramChatMigrationResult:
    _validate_telegram_chat_id(
        old_telegram_chat_id,
        field_name="old Telegram chat id",
    )
    _validate_telegram_chat_id(
        new_telegram_chat_id,
        field_name="new Telegram chat id",
    )
    if old_telegram_chat_id == new_telegram_chat_id:
        raise TelegramChatMigrationError("Telegram chat ids must be different.")

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing_migration = connection.execute(
            """
            SELECT new_telegram_chat_id
            FROM telegram_chat_migrations
            WHERE old_telegram_chat_id = ?
            """,
            (old_telegram_chat_id,),
        ).fetchone()
        if existing_migration is not None:
            recorded_new_chat_id = int(existing_migration["new_telegram_chat_id"])
            if recorded_new_chat_id != new_telegram_chat_id:
                raise TelegramChatMigrationError(
                    "Telegram chat was already migrated to another chat."
                )
            destination_row = _get_chat_row(
                connection,
                telegram_chat_id=new_telegram_chat_id,
            )
            if destination_row is None:
                raise RuntimeError("Migrated Telegram chat destination was not found.")
            _update_chat_title(
                connection,
                chat_id=int(destination_row["id"]),
                new_chat_title=new_chat_title,
            )
            return TelegramChatMigrationResult(
                chat_id=int(destination_row["id"]),
                old_telegram_chat_id=old_telegram_chat_id,
                new_telegram_chat_id=new_telegram_chat_id,
                migrated_audit_event_count=0,
                already_migrated=True,
            )

        source_row = _get_chat_row(
            connection,
            telegram_chat_id=old_telegram_chat_id,
        )
        if source_row is None:
            raise TelegramChatMigrationError("Source Telegram chat was not found.")
        if (
            _get_chat_row(
                connection,
                telegram_chat_id=new_telegram_chat_id,
            )
            is not None
        ):
            raise TelegramChatMigrationError(
                "Destination Telegram chat already exists."
            )

        chat_id = int(source_row["id"])
        connection.execute(
            """
            UPDATE chats
            SET telegram_chat_id = ?,
                title = CASE
                    WHEN ? IS NULL OR ? = '' THEN title
                    ELSE ?
                END
            WHERE id = ?
            """,
            (
                new_telegram_chat_id,
                new_chat_title,
                new_chat_title,
                new_chat_title,
                chat_id,
            ),
        )
        audit_update = connection.execute(
            """
            UPDATE audit_events
            SET chat_id = ?
            WHERE chat_id = ?
            """,
            (new_telegram_chat_id, old_telegram_chat_id),
        )
        connection.execute(
            """
            INSERT INTO telegram_chat_migrations (
                old_telegram_chat_id,
                new_telegram_chat_id
            )
            VALUES (?, ?)
            """,
            (old_telegram_chat_id, new_telegram_chat_id),
        )

        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_violations:
            raise RuntimeError(
                "Telegram chat migration introduced foreign key violations."
            )

        return TelegramChatMigrationResult(
            chat_id=chat_id,
            old_telegram_chat_id=old_telegram_chat_id,
            new_telegram_chat_id=new_telegram_chat_id,
            migrated_audit_event_count=audit_update.rowcount,
            already_migrated=False,
        )


def is_telegram_chat_migrated(
    *,
    database_path: Path,
    telegram_chat_id: int,
) -> bool:
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM telegram_chat_migrations
            WHERE old_telegram_chat_id = ?
            """,
            (telegram_chat_id,),
        ).fetchone()
    return row is not None


def _get_chat_row(
    connection: sqlite3.Connection,
    *,
    telegram_chat_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id
        FROM chats
        WHERE telegram_chat_id = ?
        """,
        (telegram_chat_id,),
    ).fetchone()


def _update_chat_title(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    new_chat_title: str | None,
) -> None:
    if not new_chat_title:
        return
    connection.execute(
        "UPDATE chats SET title = ? WHERE id = ?",
        (new_chat_title, chat_id),
    )


def _validate_telegram_chat_id(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise TelegramChatMigrationError(
            f"{field_name.capitalize()} must be a non-zero integer."
        )
