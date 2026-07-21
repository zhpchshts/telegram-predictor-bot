from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import errors, types

from app.telegram_username_resolver import (
    TelethonTelegramUsernameResolver,
    UsernameFloodWaitError,
    UsernameInvalidError,
    UsernameNotFoundError,
    UsernameResolutionUnavailableError,
    UsernameTargetNotSupportedError,
    normalize_username,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("valid_name", "valid_name"), ("@Valid_Name", "Valid_Name")],
)
def test_normalize_username_accepts_exact_username(value: str, expected: str) -> None:
    assert normalize_username(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "@", "abcd", "1username", "user name", "@user-name", "@@username"],
)
def test_normalize_username_rejects_invalid_values(value: str) -> None:
    with pytest.raises(UsernameInvalidError):
        normalize_username(value)


class FakeTelethonClient:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.requests: list[object] = []
        self.disconnected = False

    def is_connected(self) -> bool:
        return True

    async def __call__(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result

    async def disconnect(self) -> None:
        self.disconnected = True


class RetryingTelethonClient(FakeTelethonClient):
    def __init__(self, result) -> None:
        super().__init__(result)
        self.connected = False
        self.start_calls = 0

    def is_connected(self) -> bool:
        return self.connected

    async def start(self, *, bot_token: str) -> None:
        assert bot_token == "dummy"
        self.start_calls += 1
        if self.start_calls == 1:
            raise OSError("temporary connection failure")
        self.connected = True


class ConcurrentStartTelethonClient(FakeTelethonClient):
    def __init__(self, result) -> None:
        super().__init__(result)
        self.connected = False
        self.start_calls = 0
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()

    def is_connected(self) -> bool:
        return self.connected

    async def start(self, *, bot_token: str) -> None:
        assert bot_token == "dummy"
        self.start_calls += 1
        self.start_entered.set()
        await self.release_start.wait()
        self.connected = True


def build_resolver(client: FakeTelethonClient) -> TelethonTelegramUsernameResolver:
    resolver = object.__new__(TelethonTelegramUsernameResolver)
    resolver._client = client
    resolver._bot_token = "dummy"
    resolver._connection_lock = asyncio.Lock()
    resolver._authorized = True
    return resolver


def test_resolver_returns_regular_user_from_exact_request() -> None:
    client = FakeTelethonClient(
        SimpleNamespace(
            peer=types.PeerUser(user_id=456),
            users=[
                types.User(
                    id=456,
                    access_hash=1,
                    username="CanonicalName",
                    first_name="Имя",
                    last_name="Фамилия",
                )
            ],
        )
    )

    result = asyncio.run(build_resolver(client).resolve_username("@canonicalname"))

    assert result.telegram_user_id == 456
    assert result.username == "CanonicalName"
    assert result.first_name == "Имя"
    assert result.last_name == "Фамилия"
    assert len(client.requests) == 1
    assert client.requests[0].username == "canonicalname"


def test_resolver_retries_connection_after_initial_failure() -> None:
    client = RetryingTelethonClient(
        SimpleNamespace(
            peer=types.PeerUser(user_id=456),
            users=[
                types.User(
                    id=456,
                    access_hash=1,
                    username="username",
                    first_name="Имя",
                )
            ],
        )
    )
    resolver = build_resolver(client)
    resolver._authorized = False

    async def exercise() -> None:
        await resolver.start()
        result = await resolver.resolve_username("username")
        assert result.telegram_user_id == 456

    asyncio.run(exercise())

    assert client.start_calls == 2


def test_concurrent_resolutions_start_shared_client_once() -> None:
    result = SimpleNamespace(
        peer=types.PeerUser(user_id=456),
        users=[
            types.User(
                id=456,
                access_hash=1,
                username="username",
                first_name="Имя",
            )
        ],
    )

    async def exercise() -> tuple[ConcurrentStartTelethonClient, list[object]]:
        client = ConcurrentStartTelethonClient(result)
        resolver = build_resolver(client)
        resolver._authorized = False
        resolutions = [
            asyncio.create_task(resolver.resolve_username("username"))
            for _index in range(2)
        ]
        await client.start_entered.wait()
        client.release_start.set()
        return client, await asyncio.gather(*resolutions)

    client, resolutions = asyncio.run(exercise())

    assert client.start_calls == 1
    assert len(client.requests) == 2
    assert all(result.telegram_user_id == 456 for result in resolutions)


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(peer=types.PeerChannel(channel_id=1), users=[]),
        SimpleNamespace(
            peer=types.PeerUser(user_id=2),
            users=[types.User(id=2, access_hash=1, bot=True, first_name="Bot")],
        ),
        SimpleNamespace(
            peer=types.PeerUser(user_id=3),
            users=[types.User(id=3, access_hash=1, deleted=True)],
        ),
    ],
)
def test_resolver_rejects_unsupported_peer(result) -> None:
    with pytest.raises(UsernameTargetNotSupportedError):
        asyncio.run(
            build_resolver(FakeTelethonClient(result)).resolve_username("username")
        )


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (errors.UsernameNotOccupiedError(request=None), UsernameNotFoundError),
        (errors.UsernameInvalidError(request=None), UsernameInvalidError),
        (OSError("private network detail"), UsernameResolutionUnavailableError),
    ],
)
def test_resolver_maps_telethon_errors(
    error: Exception, expected_error: type[Exception]
) -> None:
    with pytest.raises(expected_error):
        asyncio.run(
            build_resolver(FakeTelethonClient(error=error)).resolve_username("username")
        )


def test_resolver_maps_flood_wait_with_retry_after() -> None:
    with pytest.raises(UsernameFloodWaitError) as error_info:
        asyncio.run(
            build_resolver(
                FakeTelethonClient(
                    error=errors.FloodWaitError(request=None, capture=17)
                )
            ).resolve_username("username")
        )

    assert error_info.value.retry_after == 17


def test_resolver_uses_single_persistent_session_path_and_disables_updates(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "telegram-mtproto"

    resolver = TelethonTelegramUsernameResolver(
        api_id=12345,
        api_hash="test-hash",
        bot_token="123:test",
        session_path=session_path,
    )

    assert (tmp_path / "telegram-mtproto.session").exists()
    assert not (tmp_path / "telegram-mtproto.session.session").exists()
    assert resolver._client._no_updates is True
    asyncio.run(resolver.close())
