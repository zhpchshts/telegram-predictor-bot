from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.audit_service import (
    AuditActor,
    AuditEntityType,
    AuditEventType,
    record_audit_event,
)
from app.database import database_connection
from app.user_service import LocalUser


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


@dataclass(frozen=True, slots=True)
class ActiveSupermoderatorAssignment:
    assignment: SupermoderatorAssignment
    user: LocalUser
    assigned_by: LocalUser


@dataclass(frozen=True, slots=True)
class SupermoderatorAssignmentResult:
    assignment: SupermoderatorAssignment
    was_created: bool


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
    return assign_supermoderator_with_status(
        database_path=database_path,
        chat_id=chat_id,
        user_id=user_id,
        assigned_by_user_id=assigned_by_user_id,
    ).assignment


def assign_supermoderator_with_status(
    *,
    database_path: Path,
    chat_id: int,
    user_id: int,
    assigned_by_user_id: int,
    audit_actor: AuditActor | None = None,
) -> SupermoderatorAssignmentResult:
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing_row = _get_active_assignment_row(
            connection,
            chat_id=chat_id,
            user_id=user_id,
        )
        if existing_row is not None:
            return SupermoderatorAssignmentResult(
                assignment=_assignment_from_row(existing_row),
                was_created=False,
            )

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
            return SupermoderatorAssignmentResult(
                assignment=_assignment_from_row(existing_row),
                was_created=False,
            )

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
        assignment = _assignment_from_row(row)
        if audit_actor is not None:
            _validate_audit_actor_chat(
                connection,
                chat_id=chat_id,
                audit_actor=audit_actor,
            )
            target_telegram_user_id = _get_telegram_user_id(
                connection,
                user_id=user_id,
            )
            record_audit_event(
                connection,
                actor=audit_actor,
                event_type=AuditEventType.SUPERMODERATOR_ASSIGNED,
                entity_type=AuditEntityType.SUPERMODERATOR_ASSIGNMENT,
                entity_id=assignment.id,
                contest_id=None,
                before_state=None,
                after_state=_assignment_snapshot(
                    assignment,
                    target_telegram_user_id=target_telegram_user_id,
                ),
                metadata={"target_telegram_user_id": target_telegram_user_id},
            )
        return SupermoderatorAssignmentResult(
            assignment=assignment,
            was_created=True,
        )


def list_active_supermoderator_assignments(
    *,
    database_path: Path,
    chat_id: int,
) -> list[ActiveSupermoderatorAssignment]:
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                assignment.*,
                target.telegram_user_id AS target_telegram_user_id,
                target.username AS target_username,
                target.first_name AS target_first_name,
                target.last_name AS target_last_name,
                actor.telegram_user_id AS actor_telegram_user_id,
                actor.username AS actor_username,
                actor.first_name AS actor_first_name,
                actor.last_name AS actor_last_name
            FROM supermoderator_assignments AS assignment
            JOIN users AS target ON target.id = assignment.user_id
            JOIN users AS actor ON actor.id = assignment.assigned_by_user_id
            WHERE assignment.chat_id = ? AND assignment.revoked_at IS NULL
            ORDER BY assignment.id
            """,
            (chat_id,),
        ).fetchall()
    return [
        ActiveSupermoderatorAssignment(
            assignment=_assignment_from_row(row),
            user=LocalUser(
                id=int(row["user_id"]),
                telegram_user_id=int(row["target_telegram_user_id"]),
                username=(
                    str(row["target_username"])
                    if row["target_username"] is not None
                    else None
                ),
                first_name=str(row["target_first_name"]),
                last_name=(
                    str(row["target_last_name"])
                    if row["target_last_name"] is not None
                    else None
                ),
            ),
            assigned_by=LocalUser(
                id=int(row["assigned_by_user_id"]),
                telegram_user_id=int(row["actor_telegram_user_id"]),
                username=(
                    str(row["actor_username"])
                    if row["actor_username"] is not None
                    else None
                ),
                first_name=str(row["actor_first_name"]),
                last_name=(
                    str(row["actor_last_name"])
                    if row["actor_last_name"] is not None
                    else None
                ),
            ),
        )
        for row in rows
    ]


def revoke_supermoderator(
    *,
    database_path: Path,
    chat_id: int,
    user_id: int,
    revoked_by_user_id: int,
    audit_actor: AuditActor | None = None,
) -> SupermoderatorAssignment:
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        previous_row = _get_active_assignment_row(
            connection,
            chat_id=chat_id,
            user_id=user_id,
        )
        if previous_row is None:
            raise SupermoderatorAssignmentNotFoundError(
                "Active supermoderator assignment was not found."
            )
        previous_assignment = _assignment_from_row(previous_row)
        cursor = connection.execute(
            """
            UPDATE supermoderator_assignments
            SET
                revoked_by_user_id = ?,
                revoked_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND revoked_at IS NULL
            """,
            (revoked_by_user_id, previous_assignment.id),
        )
        if cursor.rowcount != 1:
            raise SupermoderatorAssignmentNotFoundError(
                "Active supermoderator assignment was not found."
            )

        row = connection.execute(
            """
            SELECT *
            FROM supermoderator_assignments
            WHERE id = ?
            """,
            (previous_assignment.id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Revoked supermoderator assignment was not found.")
        assignment = _assignment_from_row(row)
        if audit_actor is not None:
            _validate_audit_actor_chat(
                connection,
                chat_id=chat_id,
                audit_actor=audit_actor,
            )
            target_telegram_user_id = _get_telegram_user_id(
                connection,
                user_id=user_id,
            )
            record_audit_event(
                connection,
                actor=audit_actor,
                event_type=AuditEventType.SUPERMODERATOR_REVOKED,
                entity_type=AuditEntityType.SUPERMODERATOR_ASSIGNMENT,
                entity_id=assignment.id,
                contest_id=None,
                before_state=_assignment_snapshot(
                    previous_assignment,
                    target_telegram_user_id=target_telegram_user_id,
                ),
                after_state=_assignment_snapshot(
                    assignment,
                    target_telegram_user_id=target_telegram_user_id,
                ),
                metadata={"target_telegram_user_id": target_telegram_user_id},
            )
        return assignment


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


def _assignment_snapshot(
    assignment: SupermoderatorAssignment,
    *,
    target_telegram_user_id: int,
) -> dict[str, object]:
    return {
        "assigned_at": assignment.assigned_at,
        "assigned_by_user_id": assignment.assigned_by_user_id,
        "assignment_id": assignment.id,
        "chat_id": assignment.chat_id,
        "revoked_at": assignment.revoked_at,
        "revoked_by_user_id": assignment.revoked_by_user_id,
        "target_telegram_user_id": target_telegram_user_id,
        "user_id": assignment.user_id,
    }


def _get_telegram_user_id(
    connection: sqlite3.Connection,
    *,
    user_id: int,
) -> int:
    row = connection.execute(
        "SELECT telegram_user_id FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Supermoderator target user was not found.")
    return int(row["telegram_user_id"])


def _validate_audit_actor_chat(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    audit_actor: AuditActor,
) -> None:
    row = connection.execute(
        "SELECT telegram_chat_id FROM chats WHERE id = ?",
        (chat_id,),
    ).fetchone()
    if row is None or int(row["telegram_chat_id"]) != audit_actor.telegram_chat_id:
        raise ValueError("Audit actor chat does not match assignment chat.")
