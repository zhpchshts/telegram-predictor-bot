from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


logger = logging.getLogger(__name__)
USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


class UsernameInvalidError(ValueError):
    pass


class UsernameNotFoundError(LookupError):
    pass


class UsernameTargetNotSupportedError(ValueError):
    pass


class UsernameResolutionUnavailableError(RuntimeError):
    pass


class UsernameResolutionNotConfiguredError(UsernameResolutionUnavailableError):
    pass


class UsernameFloodWaitError(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Telegram username resolution is rate limited.")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class ResolvedTelegramUser:
    telegram_user_id: int
    username: str
    first_name: str | None
    last_name: str | None


class TelegramUsernameResolver(Protocol):
    async def resolve_username(self, username: str) -> ResolvedTelegramUser: ...

    async def close(self) -> None: ...


def normalize_username(value: str) -> str:
    normalized = value.removeprefix("@")
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise UsernameInvalidError("Telegram username has an invalid format.")
    return normalized


class UnavailableTelegramUsernameResolver:
    async def resolve_username(self, username: str) -> ResolvedTelegramUser:
        normalize_username(username)
        raise UsernameResolutionNotConfiguredError(
            "Telegram username resolution is not configured."
        )

    async def close(self) -> None:
        return None


class TelethonTelegramUsernameResolver:
    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        bot_token: str,
        session_path: Path,
    ) -> None:
        from telethon import TelegramClient

        session_path.parent.mkdir(parents=True, exist_ok=True)
        self._client = TelegramClient(
            str(session_path),
            api_id,
            api_hash,
            receive_updates=False,
        )
        self._bot_token = bot_token
        self._connection_lock = asyncio.Lock()
        self._authorized = False

    async def start(self) -> None:
        try:
            await self._ensure_connected()
        except UsernameResolutionUnavailableError:
            logger.warning(
                "Initial Telegram MTProto connection failed; username resolution "
                "will retry on demand."
            )

    async def resolve_username(self, username: str) -> ResolvedTelegramUser:
        normalized_username = normalize_username(username)
        await self._ensure_connected()

        from telethon import errors, functions, types

        try:
            result = await self._client(
                functions.contacts.ResolveUsernameRequest(normalized_username)
            )
        except errors.UsernameNotOccupiedError as error:
            raise UsernameNotFoundError("Telegram username is not occupied.") from error
        except errors.UsernameInvalidError as error:
            raise UsernameInvalidError(
                "Telegram username has an invalid format."
            ) from error
        except errors.FloodWaitError as error:
            raise UsernameFloodWaitError(max(1, int(error.seconds))) from error
        except Exception as error:
            self._authorized = False
            try:
                await self._client.disconnect()
            except Exception:
                logger.warning("Telegram MTProto disconnect after failure failed.")
            logger.warning(
                "Telegram MTProto username resolution failed (%s).",
                type(error).__name__,
            )
            raise UsernameResolutionUnavailableError(
                "Telegram username resolution is unavailable."
            ) from error

        if not isinstance(result.peer, types.PeerUser):
            raise UsernameTargetNotSupportedError(
                "Resolved Telegram peer is not a user."
            )
        user = next(
            (
                candidate
                for candidate in result.users
                if isinstance(candidate, types.User)
                and candidate.id == result.peer.user_id
            ),
            None,
        )
        if user is None or user.bot or user.deleted:
            raise UsernameTargetNotSupportedError(
                "Resolved Telegram peer is not a supported user."
            )
        resolved_username = user.username or normalized_username
        return ResolvedTelegramUser(
            telegram_user_id=int(user.id),
            username=str(resolved_username),
            first_name=str(user.first_name) if user.first_name else None,
            last_name=str(user.last_name) if user.last_name else None,
        )

    async def close(self) -> None:
        await self._client.disconnect()

    async def _ensure_connected(self) -> None:
        if self._client.is_connected() and self._authorized:
            return
        async with self._connection_lock:
            if self._client.is_connected() and self._authorized:
                return
            try:
                await self._client.start(bot_token=self._bot_token)
                self._authorized = True
            except Exception as error:
                logger.warning(
                    "Telegram MTProto bot authorization failed (%s).",
                    type(error).__name__,
                )
                raise UsernameResolutionUnavailableError(
                    "Telegram username resolution is unavailable."
                ) from error
