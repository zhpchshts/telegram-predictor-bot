from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.audit_service import AuditActor, AuditActorRole
from app.database import create_connection, initialize_database
from app import supermoderator_service
from app.supermoderator_service import (
    SupermoderatorAssignmentNotFoundError,
    assign_supermoderator,
    assign_supermoderator_with_status,
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
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    chat_id, user_id, actor_id = _create_chat_and_users(database_path)
    start_barrier = threading.Barrier(2, timeout=5)

    def assign():
        start_barrier.wait()
        return assign_supermoderator(
            database_path=database_path,
            chat_id=chat_id,
            user_id=user_id,
            assigned_by_user_id=actor_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(assign) for _ in range(2)]
        assignments = [future.result(timeout=10) for future in futures]

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


def test_concurrent_revoke_and_reassignment_keep_audit_snapshots_consistent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    chat_id, user_id, actor_id = _create_chat_and_users(database_path)
    initial_assignment = assign_supermoderator(
        database_path=database_path,
        chat_id=chat_id,
        user_id=user_id,
        assigned_by_user_id=actor_id,
    )
    audit_actor = AuditActor(
        telegram_chat_id=-100123,
        telegram_user_id=456,
        role=AuditActorRole.TELEGRAM_ADMIN,
    )

    operation_context = threading.local()
    first_revoke_read = threading.Event()
    release_first_revoke = threading.Event()
    second_transaction_started = threading.Event()
    original_database_connection = supermoderator_service.database_connection
    original_get_active_assignment_row = (
        supermoderator_service._get_active_assignment_row
    )

    @contextmanager
    def coordinated_database_connection(path: Path):
        with original_database_connection(path) as connection:

            def trace_statement(statement: str) -> None:
                if (
                    getattr(operation_context, "name", None) == "reassign"
                    and statement.strip().upper() == "BEGIN IMMEDIATE"
                ):
                    second_transaction_started.set()

            connection.set_trace_callback(trace_statement)
            yield connection

    def coordinated_get_active_assignment_row(*args, **kwargs):
        row = original_get_active_assignment_row(*args, **kwargs)
        if getattr(operation_context, "name", None) == "first_revoke":
            first_revoke_read.set()
            if not release_first_revoke.wait(timeout=5):
                raise RuntimeError("Timed out while pausing the first revocation.")
        return row

    monkeypatch.setattr(
        supermoderator_service,
        "database_connection",
        coordinated_database_connection,
    )
    monkeypatch.setattr(
        supermoderator_service,
        "_get_active_assignment_row",
        coordinated_get_active_assignment_row,
    )

    def first_revoke():
        operation_context.name = "first_revoke"
        return revoke_supermoderator(
            database_path=database_path,
            chat_id=chat_id,
            user_id=user_id,
            revoked_by_user_id=actor_id,
            audit_actor=audit_actor,
        )

    def revoke_then_reassign():
        operation_context.name = "reassign"
        try:
            revoke_supermoderator(
                database_path=database_path,
                chat_id=chat_id,
                user_id=user_id,
                revoked_by_user_id=actor_id,
                audit_actor=audit_actor,
            )
        except SupermoderatorAssignmentNotFoundError:
            pass
        return assign_supermoderator_with_status(
            database_path=database_path,
            chat_id=chat_id,
            user_id=user_id,
            assigned_by_user_id=actor_id,
            audit_actor=audit_actor,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_revoke)
        assert first_revoke_read.wait(timeout=5)
        second_future = executor.submit(revoke_then_reassign)
        try:
            assert second_transaction_started.wait(timeout=5)
            assert not second_future.done()
        finally:
            release_first_revoke.set()

        first_result = first_future.result(timeout=5)
        second_result = second_future.result(timeout=5)

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
        events = connection.execute(
            """
            SELECT event_type, entity_id, before_state, after_state
            FROM audit_events
            ORDER BY id
            """
        ).fetchall()

    assert first_result.id == initial_assignment.id
    assert second_result.was_created is True
    assert second_result.assignment.id != initial_assignment.id
    assert [(row["id"], row["revoked_at"] is None) for row in history] == [
        (initial_assignment.id, False),
        (second_result.assignment.id, True),
    ]
    assert [event["event_type"] for event in events] == [
        "supermoderator_revoked",
        "supermoderator_assigned",
    ]
    revoked_before = json.loads(events[0]["before_state"])
    revoked_after = json.loads(events[0]["after_state"])
    assigned_after = json.loads(events[1]["after_state"])
    assert events[0]["entity_id"] == initial_assignment.id
    assert revoked_before["assignment_id"] == initial_assignment.id
    assert revoked_after["assignment_id"] == initial_assignment.id
    assert events[1]["entity_id"] == second_result.assignment.id
    assert assigned_after["assignment_id"] == second_result.assignment.id


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
