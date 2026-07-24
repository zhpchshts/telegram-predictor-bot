from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner

from app.supermoderator_service import (
    get_active_supermoderator_assignment_by_telegram_ids,
)


class TelegramAdministratorsClient(Protocol):
    async def get_chat_administrators(
        self,
        chat_id: int,
    ) -> list[ChatMemberOwner | ChatMemberAdministrator]: ...


class AccessVerificationStatus(StrEnum):
    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"


class AccessRole(StrEnum):
    TELEGRAM_ADMIN = "telegram_admin"
    SUPERMODERATOR = "supermoderator"
    PARTICIPANT = "participant"


@dataclass(frozen=True, slots=True)
class AccessDecision:
    verification_status: AccessVerificationStatus
    role: AccessRole | None
    can_manage_contests: bool
    can_manage_roles: bool
    enforcement_enabled: bool
    administrators: TelegramAdministratorsSnapshot | None


class TelegramAdministratorsUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramAdministratorsSnapshot:
    telegram_user_ids: frozenset[int]

    def contains(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self.telegram_user_ids


async def get_telegram_administrators_snapshot(
    *,
    telegram_chat_id: int,
    telegram_client: TelegramAdministratorsClient,
) -> TelegramAdministratorsSnapshot:
    try:
        administrators = await telegram_client.get_chat_administrators(
            chat_id=telegram_chat_id
        )
    except TelegramAPIError as error:
        raise TelegramAdministratorsUnavailableError(
            "Telegram administrators are unavailable."
        ) from error
    return TelegramAdministratorsSnapshot(
        telegram_user_ids=frozenset(
            int(administrator.user.id) for administrator in administrators
        )
    )


async def determine_access(
    *,
    database_path: Path,
    telegram_chat_id: int,
    telegram_user_id: int,
    telegram_client: TelegramAdministratorsClient,
    enforcement_enabled: bool,
) -> AccessDecision:
    local_assignment = get_active_supermoderator_assignment_by_telegram_ids(
        database_path=database_path,
        telegram_chat_id=telegram_chat_id,
        telegram_user_id=telegram_user_id,
    )

    try:
        administrators = await get_telegram_administrators_snapshot(
            telegram_chat_id=telegram_chat_id,
            telegram_client=telegram_client,
        )
    except TelegramAdministratorsUnavailableError:
        role = AccessRole.SUPERMODERATOR if local_assignment is not None else None
        return _build_access_decision(
            verification_status=AccessVerificationStatus.UNAVAILABLE,
            role=role,
            enforcement_enabled=enforcement_enabled,
            administrators=None,
        )

    is_telegram_admin = administrators.contains(telegram_user_id)
    if is_telegram_admin:
        role = AccessRole.TELEGRAM_ADMIN
    elif local_assignment is not None:
        role = AccessRole.SUPERMODERATOR
    else:
        role = AccessRole.PARTICIPANT

    return _build_access_decision(
        verification_status=AccessVerificationStatus.VERIFIED,
        role=role,
        enforcement_enabled=enforcement_enabled,
        administrators=administrators,
    )


def _build_access_decision(
    *,
    verification_status: AccessVerificationStatus,
    role: AccessRole | None,
    enforcement_enabled: bool,
    administrators: TelegramAdministratorsSnapshot | None,
) -> AccessDecision:
    can_manage_contests = role in {
        AccessRole.TELEGRAM_ADMIN,
        AccessRole.SUPERMODERATOR,
    }
    can_manage_roles = (
        verification_status is AccessVerificationStatus.VERIFIED
        and role is AccessRole.TELEGRAM_ADMIN
    )
    return AccessDecision(
        verification_status=verification_status,
        role=role,
        can_manage_contests=can_manage_contests,
        can_manage_roles=can_manage_roles,
        enforcement_enabled=enforcement_enabled,
        administrators=administrators,
    )
