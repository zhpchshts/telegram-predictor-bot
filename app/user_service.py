from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.database import database_connection


@dataclass(frozen=True, slots=True)
class LocalUser:
    id: int
    telegram_user_id: int
    username: str | None
    first_name: str
    last_name: str | None


@dataclass(frozen=True, slots=True)
class ChatActor:
    chat_id: int
    actor_user_id: int


def upsert_telegram_user(
    *,
    database_path: Path,
    telegram_user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> LocalUser:
    with database_connection(database_path) as connection:
        user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        row = _get_user_row(connection, user_id=user_id)
    if row is None:
        raise RuntimeError("Upserted Telegram user was not found.")
    return _user_from_row(row)


def upsert_chat_actor(
    *,
    database_path: Path,
    telegram_chat_id: int,
    chat_title: str | None,
    telegram_user_id: int,
    username: str | None,
    first_name: str,
    last_name: str | None,
) -> ChatActor:
    with database_connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO chats (telegram_chat_id, title)
            VALUES (?, ?)
            ON CONFLICT(telegram_chat_id) DO UPDATE SET title = excluded.title
            """,
            (telegram_chat_id, chat_title or "Без названия"),
        )
        chat_row = connection.execute(
            "SELECT id FROM chats WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        ).fetchone()
        if chat_row is None:
            raise RuntimeError("Upserted Telegram chat was not found.")
        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        return ChatActor(
            chat_id=int(chat_row["id"]),
            actor_user_id=actor_user_id,
        )


def get_user_by_telegram_id(
    *,
    database_path: Path,
    telegram_user_id: int,
) -> LocalUser | None:
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT id, telegram_user_id, username, first_name, last_name
            FROM users
            WHERE telegram_user_id = ?
            """,
            (telegram_user_id,),
        ).fetchone()
    return _user_from_row(row) if row is not None else None


def get_or_create_telegram_user(
    *,
    database_path: Path,
    telegram_user_id: int,
) -> LocalUser:
    if telegram_user_id <= 0:
        raise ValueError("Telegram user id must be a positive integer.")
    with database_connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users (telegram_user_id, username, first_name, last_name)
            VALUES (?, NULL, '', NULL)
            ON CONFLICT(telegram_user_id) DO NOTHING
            """,
            (telegram_user_id,),
        )
        row = connection.execute(
            """
            SELECT id, telegram_user_id, username, first_name, last_name
            FROM users
            WHERE telegram_user_id = ?
            """,
            (telegram_user_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("Created Telegram user was not found.")
    return _user_from_row(row)


def _upsert_user(
    connection: sqlite3.Connection,
    *,
    telegram_user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> int:
    safe_first_name = first_name or ""
    connection.execute(
        """
        INSERT INTO users (telegram_user_id, username, first_name, last_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            username = excluded.username,
            first_name = CASE
                WHEN excluded.first_name = '' THEN users.first_name
                ELSE excluded.first_name
            END,
            last_name = excluded.last_name
        """,
        (telegram_user_id, username, safe_first_name, last_name),
    )
    row = connection.execute(
        "SELECT id FROM users WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Upserted Telegram user id was not found.")
    return int(row["id"])


def _get_user_row(
    connection: sqlite3.Connection,
    *,
    user_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, telegram_user_id, username, first_name, last_name
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()


def _user_from_row(row: sqlite3.Row) -> LocalUser:
    return LocalUser(
        id=int(row["id"]),
        telegram_user_id=int(row["telegram_user_id"]),
        username=str(row["username"]) if row["username"] is not None else None,
        first_name=str(row["first_name"]),
        last_name=str(row["last_name"]) if row["last_name"] is not None else None,
    )
