from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.database import database_connection


@dataclass(frozen=True, slots=True)
class ActiveContestSummary:
    id: int
    name: str
    slug: str
    created_at: str


def get_active_contests(
    *,
    database_path: Path,
    telegram_chat_id: int,
) -> tuple[ActiveContestSummary, ...]:
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                contests.id,
                contests.name,
                contests.slug,
                contests.created_at
            FROM contests
            JOIN chats
                ON chats.id = contests.chat_id
            WHERE chats.telegram_chat_id = ?
                AND contests.is_active = 1
            ORDER BY contests.created_at DESC, contests.id DESC
            """,
            (telegram_chat_id,),
        ).fetchall()

    return tuple(
        ActiveContestSummary(
            id=int(row["id"]),
            name=str(row["name"]),
            slug=str(row["slug"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    )
