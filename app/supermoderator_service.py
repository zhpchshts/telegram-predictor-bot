from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.database import database_connection


class SupermoderatorAssignmentNotFoundError(ValueError):
    """Raised when there is no active assignment to revoke."""


@dataclass(frozen=True, slots=True)
class SupermoderatorAssignment:
    id: int
    chat_id: int
    user_id: int
    assigned_by_user_id: int
    assigned_at: str
    revoked_by_user_id: int | None
    revoked_at: str | None


def get_active_supermoderator_assignment(
    *,
    database_path: Path,
    chat_id: int,
    user_id: int,
) -> SupermoderatorAssignment | None:
    with database_connection(database_path) as connection:
        row = _get_active_assignment_row(
            connection,
            chat_id=chat_id,
            user_id=user_id,
        )
    return _assignment_from_row(row) if row is not None else None


def get_active_supermoderator_assignment_by_telegram_ids(
    *,
    database_path: Path,
    telegram_chat_id: int,
    telegram_user_id: int,
) -> SupermoderatorAssignment | None:
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT assignment.*
            FROM supermoderator_assignments AS assignment
            JOIN chats AS chat ON chat.id = assignment.chat_id
            JOIN users AS user ON user.id = assignment.user_id
            WHERE chat.telegram_chat_id = ?
              AND user.telegram_user_id = ?
              AND assignment.revoked_at IS NULL
            """,
            (telegram_chat_id, telegram_user_id),
        ).fetchone()
    return _assignment_from_row(row) if row is not None else None


def assign_supermoderator(
    *,
    database_path: Path,
    chat_id: int,
    user_id: int,
    assigned_by_user_id: int,
) -> SupermoderatorAssignment:
    with database_connection(database_path) as connection:
        existing_row = _get_active_assignment_row(
            connection,
            chat_id=chat_id,
            user_id=user_id,
        )
        if existing_row is not None:
            return _assignment_from_row(existing_row)

        try:
            assignment_id = int(
                connection.execute(
                    """
                    INSERT INTO supermoderator_assignments (
                        chat_id,
                        user_id,
                        assigned_by_user_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (chat_id, user_id, assigned_by_user_id),
                ).lastrowid
            )
        except sqlite3.IntegrityError:
            existing_row = _get_active_assignment_row(
                connection,
                chat_id=chat_id,
                user_id=user_id,
            )
            if existing_row is None:
                raise
            return _assignment_from_row(existing_row)

        row = connection.execute(
            """
            SELECT *
            FROM supermoderator_assignments
            WHERE id = ?
            """,
            (assignment_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Created supermoderator assignment was not found.")
        return _assignment_from_row(row)


def revoke_supermoderator(
    *,
    database_path: Path,
    chat_id: int,
    user_id: int,
    revoked_by_user_id: int,
) -> SupermoderatorAssignment:
    with database_connection(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE supermoderator_assignments
            SET
                revoked_by_user_id = ?,
                revoked_at = CURRENT_TIMESTAMP
            WHERE chat_id = ?
              AND user_id = ?
              AND revoked_at IS NULL
            """,
            (revoked_by_user_id, chat_id, user_id),
        )
        if cursor.rowcount != 1:
            raise SupermoderatorAssignmentNotFoundError(
                "Active supermoderator assignment was not found."
            )

        row = connection.execute(
            """
            SELECT *
            FROM supermoderator_assignments
            WHERE chat_id = ? AND user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, user_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Revoked supermoderator assignment was not found.")
        return _assignment_from_row(row)


def _get_active_assignment_row(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    user_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT *
        FROM supermoderator_assignments
        WHERE chat_id = ?
          AND user_id = ?
          AND revoked_at IS NULL
        """,
        (chat_id, user_id),
    ).fetchone()


def _assignment_from_row(row: sqlite3.Row) -> SupermoderatorAssignment:
    return SupermoderatorAssignment(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        user_id=int(row["user_id"]),
        assigned_by_user_id=int(row["assigned_by_user_id"]),
        assigned_at=str(row["assigned_at"]),
        revoked_by_user_id=(
            int(row["revoked_by_user_id"])
            if row["revoked_by_user_id"] is not None
            else None
        ),
        revoked_at=str(row["revoked_at"]) if row["revoked_at"] is not None else None,
    )
