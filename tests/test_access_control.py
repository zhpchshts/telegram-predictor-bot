from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChatAdministrators

from app.access_control import (
    AccessRole,
    AccessVerificationStatus,
    determine_access,
)
from app.audit_service import AuditActor, AuditActorRole
from app.database import create_connection, initialize_database
from app.supermoderator_service import assign_supermoderator


TELEGRAM_CHAT_ID = -100123
TELEGRAM_USER_ID = 123
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=TELEGRAM_CHAT_ID,
    telegram_user_id=456,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


class FakeTelegramClient:
    def __init__(self, administrator_ids: list[int]) -> None:
        self.administrator_ids = administrator_ids
        self.administrator_calls = 0
        self.member_calls = 0

    async def get_chat_administrators(self, chat_id: int) -> list[object]:
        assert chat_id == TELEGRAM_CHAT_ID
        self.administrator_calls += 1
        return [
            SimpleNamespace(user=SimpleNamespace(id=user_id))
            for user_id in self.administrator_ids
        ]

    async def get_chat_member(self, *_args, **_kwargs) -> None:
        self.member_calls += 1
        raise AssertionError("get_chat_member must not be called")


class UnavailableTelegramClient(FakeTelegramClient):
    async def get_chat_administrators(self, chat_id: int) -> list[object]:
        assert chat_id == TELEGRAM_CHAT_ID
        self.administrator_calls += 1
        raise TelegramNetworkError(
            method=GetChatAdministrators(chat_id=chat_id),
            message="private network detail",
        )


class HangingTelegramClient:
    def __init__(self) -> None:
        self.cancelled = False

    async def get_chat_administrators(self, chat_id: int) -> list[object]:
        assert chat_id == TELEGRAM_CHAT_ID
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _create_access_records(database_path: Path) -> tuple[int, int, int]:
    initialize_database(database_path)
    with create_connection(database_path) as connection:
        chat_id = int(
            connection.execute(
                "INSERT INTO chats (telegram_chat_id, title) VALUES (?, ?)",
                (TELEGRAM_CHAT_ID, "Test chat"),
            ).lastrowid
        )
        user_id = int(
            connection.execute(
                "INSERT INTO users (telegram_user_id, first_name) VALUES (?, ?)",
                (TELEGRAM_USER_ID, "User"),
            ).lastrowid
        )
        actor_id = int(
            connection.execute(
                "INSERT INTO users (telegram_user_id, first_name) VALUES (?, ?)",
                (456, "Actor"),
            ).lastrowid
        )
    return chat_id, user_id, actor_id


def test_telegram_administrator_has_priority_over_local_assignment(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    chat_id, user_id, actor_id = _create_access_records(database_path)
    assignment = assign_supermoderator(
        database_path=database_path,
        chat_id=chat_id,
        user_id=user_id,
        assigned_by_user_id=actor_id,
        audit_actor=AUDIT_ACTOR,
    )
    telegram_client = FakeTelegramClient([TELEGRAM_USER_ID])

    access = asyncio.run(
        determine_access(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            telegram_user_id=TELEGRAM_USER_ID,
            telegram_client=telegram_client,
            enforcement_enabled=False,
        )
    )

    assert access.verification_status is AccessVerificationStatus.VERIFIED
    assert access.role is AccessRole.TELEGRAM_ADMIN
    assert access.can_manage_contests is True
    assert access.can_manage_roles is True
    assert telegram_client.member_calls == 0
    with create_connection(database_path) as connection:
        stored_assignment = connection.execute(
            "SELECT revoked_at FROM supermoderator_assignments WHERE id = ?",
            (assignment.id,),
        ).fetchone()
    assert stored_assignment["revoked_at"] is None

    telegram_client.administrator_ids = []
    access_after_removal = asyncio.run(
        determine_access(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            telegram_user_id=TELEGRAM_USER_ID,
            telegram_client=telegram_client,
            enforcement_enabled=False,
        )
    )
    assert access_after_removal.role is AccessRole.SUPERMODERATOR


def test_verified_non_admin_role_depends_on_local_assignment(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    chat_id, user_id, actor_id = _create_access_records(database_path)
    telegram_client = FakeTelegramClient([])

    participant_access = asyncio.run(
        determine_access(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            telegram_user_id=TELEGRAM_USER_ID,
            telegram_client=telegram_client,
            enforcement_enabled=True,
        )
    )
    assert participant_access.role is AccessRole.PARTICIPANT
    assert participant_access.can_manage_contests is False
    assert participant_access.can_manage_roles is False
    assert participant_access.enforcement_enabled is True

    assign_supermoderator(
        database_path=database_path,
        chat_id=chat_id,
        user_id=user_id,
        assigned_by_user_id=actor_id,
        audit_actor=AUDIT_ACTOR,
    )
    supermoderator_access = asyncio.run(
        determine_access(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            telegram_user_id=TELEGRAM_USER_ID,
            telegram_client=telegram_client,
            enforcement_enabled=False,
        )
    )
    assert supermoderator_access.role is AccessRole.SUPERMODERATOR
    assert supermoderator_access.can_manage_contests is True
    assert supermoderator_access.can_manage_roles is False
    assert telegram_client.member_calls == 0


def test_telegram_unavailable_is_distinct_and_preserves_local_role(
    tmp_path: Path,
) -> None:
    without_assignment_path = tmp_path / "without.db"
    _create_access_records(without_assignment_path)
    unavailable_client = UnavailableTelegramClient([])

    unavailable_access = asyncio.run(
        determine_access(
            database_path=without_assignment_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            telegram_user_id=TELEGRAM_USER_ID,
            telegram_client=unavailable_client,
            enforcement_enabled=False,
        )
    )
    assert (
        unavailable_access.verification_status is AccessVerificationStatus.UNAVAILABLE
    )
    assert unavailable_access.role is None
    assert unavailable_access.can_manage_contests is False
    assert unavailable_access.can_manage_roles is False
    with_assignment_path = tmp_path / "with.db"
    chat_id, user_id, actor_id = _create_access_records(with_assignment_path)
    assignment = assign_supermoderator(
        database_path=with_assignment_path,
        chat_id=chat_id,
        user_id=user_id,
        assigned_by_user_id=actor_id,
        audit_actor=AUDIT_ACTOR,
    )
    supermoderator_access = asyncio.run(
        determine_access(
            database_path=with_assignment_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            telegram_user_id=TELEGRAM_USER_ID,
            telegram_client=unavailable_client,
            enforcement_enabled=False,
        )
    )
    assert (
        supermoderator_access.verification_status
        is AccessVerificationStatus.UNAVAILABLE
    )
    assert supermoderator_access.role is AccessRole.SUPERMODERATOR
    assert supermoderator_access.can_manage_contests is True
    assert supermoderator_access.can_manage_roles is False
    with create_connection(with_assignment_path) as connection:
        stored_assignment = connection.execute(
            "SELECT revoked_at FROM supermoderator_assignments WHERE id = ?",
            (assignment.id,),
        ).fetchone()
    assert stored_assignment["revoked_at"] is None


def test_telegram_timeout_returns_unavailable_and_cancels_request(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "access.db"
    _create_access_records(database_path)
    client = HangingTelegramClient()

    access = asyncio.run(
        determine_access(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            telegram_user_id=TELEGRAM_USER_ID,
            telegram_client=client,
            enforcement_enabled=False,
            telegram_timeout_seconds=0.01,
        )
    )

    assert access.verification_status is AccessVerificationStatus.UNAVAILABLE
    assert access.role is None
    assert access.can_manage_contests is False
    assert client.cancelled is True


def test_database_errors_are_not_reported_as_telegram_unavailable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "uninitialized.db"

    with pytest.raises(Exception, match="no such table"):
        asyncio.run(
            determine_access(
                database_path=database_path,
                telegram_chat_id=TELEGRAM_CHAT_ID,
                telegram_user_id=TELEGRAM_USER_ID,
                telegram_client=UnavailableTelegramClient([]),
                enforcement_enabled=False,
            )
        )
