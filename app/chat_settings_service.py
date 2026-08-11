from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.audit_service import (
    AuditActor,
    AuditEntityType,
    AuditEventType,
    record_audit_event,
)
from app.database import database_connection


DEFAULT_APP_BUTTON_TEXT = "Открыть Клевер"
MAX_APP_BUTTON_TEXT_LENGTH = 64


@dataclass(frozen=True, slots=True)
class ChatSettings:
    app_button_text: str


def normalize_app_button_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("Button text must not be empty.")
    if len(normalized) > MAX_APP_BUTTON_TEXT_LENGTH:
        raise ValueError(
            f"Button text must not exceed {MAX_APP_BUTTON_TEXT_LENGTH} characters."
        )
    return normalized


def get_chat_settings(
    *,
    database_path: Path,
    telegram_chat_id: int,
) -> ChatSettings:
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT chat_settings.app_button_text
            FROM chat_settings
            JOIN chats ON chats.id = chat_settings.chat_id
            WHERE chats.telegram_chat_id = ?
            """,
            (telegram_chat_id,),
        ).fetchone()
    return ChatSettings(
        app_button_text=(
            str(row["app_button_text"]) if row is not None else DEFAULT_APP_BUTTON_TEXT
        )
    )


def save_chat_settings(
    *,
    database_path: Path,
    telegram_chat_id: int,
    app_button_text: str,
    actor: AuditActor,
) -> ChatSettings:
    normalized = normalize_app_button_text(app_button_text)
    with database_connection(database_path) as connection:
        chat_row = connection.execute(
            "SELECT id FROM chats WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        if chat_row is None:
            raise RuntimeError("Chat must exist before its settings can be saved.")
        chat_id = int(chat_row["id"])
        previous_row = connection.execute(
            "SELECT app_button_text FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        previous_text = (
            str(previous_row["app_button_text"])
            if previous_row is not None
            else DEFAULT_APP_BUTTON_TEXT
        )
        connection.execute(
            """
            INSERT INTO chat_settings (chat_id, app_button_text, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                app_button_text = excluded.app_button_text,
                updated_at = CURRENT_TIMESTAMP
            """,
            (chat_id, normalized),
        )
        if normalized != previous_text:
            record_audit_event(
                connection,
                actor=actor,
                event_type=AuditEventType.CHAT_SETTINGS_UPDATED,
                entity_type=AuditEntityType.CHAT_SETTINGS,
                entity_id=chat_id,
                contest_id=None,
                before_state={"app_button_text": previous_text},
                after_state={"app_button_text": normalized},
            )
    return ChatSettings(app_button_text=normalized)
