from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.database import create_connection, initialize_database
from app import supermoderator_service
from app.supermoderator_service import (
    SupermoderatorAssignmentNotFoundError,
    assign_supermoderator,
    get_active_supermoderator_assignment,
    revoke_supermoderator,
)


def _create_chat_and_users(database_path: Path) -> tuple[int, int, int]:
    with create_connection(database_path) as connection:
        chat_id = int(
            connection.execute(
                "INSERT INTO chats (telegram_chat_id, title) VALUES (?, ?)",
                (-100123, "Test chat"),
            ).lastrowid
        )
        user_id = int(
            connection.execute(
                "INSERT INTO users (telegram_user_id, first_name) VALUES (?, ?)",
                (123, "User"),
            ).lastrowid
        )
        actor_id = int(
            connection.execute(
                "INSERT INTO users (telegram_user_id, first_name) VALUES (?, ?)",
                (456, "Actor"),
            ).lastrowid
        )
    return chat_id, user_id, actor_id


def test_assignment_lifecycle_is_idempotent_and_preserves_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    initialize_database(database_path)
    chat_id, user_id, actor_id = _create_chat_and_users(database_path)

    first = assign_supermoderator(
        database_path=database_path,
        chat_id=chat_id,
        user_id=user_id,
        assigned_by_user_id=actor_id,
    )
    repeated = assign_supermoderator(
        database_path=database_path,
        chat_id=chat_id,
        user_id=user_id,
        assigned_by_user_id=actor_id,
    )

    assert repeated == first
    assert (
        get_active_supermoderator_assignment(
            database_path=database_path,
            chat_id=chat_id,
            user_id=user_id,
        )
        == first
    )

    revoked = revoke_supermoderator(
        database_path=database_path,
        chat_id=chat_id,
        user_id=user_id,
        revoked_by_user_id=actor_id,
    )
    assert revoked.id == first.id
    assert revoked.revoked_by_user_id == actor_id
    assert revoked.revoked_at is not None
    assert (
        get_active_supermoderator_assignment(
            database_path=database_path,
            chat_id=chat_id,
            user_id=user_id,
        )
        is None
    )

    second = assign_supermoderator(
        database_path=database_path,
        chat_id=chat_id,
        user_id=user_id,
        assigned_by_user_id=actor_id,
    )
    assert second.id != first.id

    with create_connection(database_path) as connection:
        history = connection.execute(
            """
            SELECT id, revoked_at
            FROM supermoderator_assignments
            WHERE chat_id = ? AND user_id = ?
            ORDER BY id
            """,
            (chat_id, user_id),
        ).fetchall()

    assert [(row["id"], row["revoked_at"] is None) for row in history] == [
        (first.id, False),
        (second.id, True),
    ]


def test_concurrent_assignment_returns_single_active_record(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    chat_id, user_id, actor_id = _create_chat_and_users(database_path)
    initial_read_barrier = threading.Barrier(2, timeout=5)
    read_count_lock = threading.Lock()
    initial_read_results: list[object] = []
    original_get_active_assignment_row = (
        supermoderator_service._get_active_assignment_row
    )

    def synchronize_initial_reads(connection, *, chat_id: int, user_id: int):
        row = original_get_active_assignment_row(
            connection,
            chat_id=chat_id,
            user_id=user_id,
        )
        with read_count_lock:
            should_wait = len(initial_read_results) < 2
            if should_wait:
                initial_read_results.append(row)
        if should_wait:
            initial_read_barrier.wait()
        return row

    monkeypatch.setattr(
        supermoderator_service,
        "_get_active_assignment_row",
        synchronize_initial_reads,
    )

    def assign():
        return assign_supermoderator(
            database_path=database_path,
            chat_id=chat_id,
            user_id=user_id,
            assigned_by_user_id=actor_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(assign) for _ in range(2)]
        assignments = [future.result(timeout=10) for future in futures]

    assert initial_read_results == [None, None]
    assert assignments[0] == assignments[1]
    with create_connection(database_path) as connection:
        active_rows = connection.execute(
            """
            SELECT id
            FROM supermoderator_assignments
            WHERE chat_id = ? AND user_id = ? AND revoked_at IS NULL
            """,
            (chat_id, user_id),
        ).fetchall()
    assert [row["id"] for row in active_rows] == [assignments[0].id]


def test_database_rejects_duplicate_active_and_partial_revocation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    chat_id, user_id, actor_id = _create_chat_and_users(database_path)

    with create_connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO supermoderator_assignments (
                chat_id, user_id, assigned_by_user_id
            ) VALUES (?, ?, ?)
            """,
            (chat_id, user_id, actor_id),
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supermoderator_assignments (
                    chat_id, user_id, assigned_by_user_id
                ) VALUES (?, ?, ?)
                """,
                (chat_id, user_id, actor_id),
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE supermoderator_assignments
                SET revoked_by_user_id = ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (actor_id, chat_id, user_id),
            )


def test_revoke_missing_assignment_has_predictable_error(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    chat_id, user_id, actor_id = _create_chat_and_users(database_path)

    with pytest.raises(
        SupermoderatorAssignmentNotFoundError,
        match="Active supermoderator assignment was not found",
    ):
        revoke_supermoderator(
            database_path=database_path,
            chat_id=chat_id,
            user_id=user_id,
            revoked_by_user_id=actor_id,
        )
