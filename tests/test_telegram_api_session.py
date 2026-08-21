from __future__ import annotations

import asyncio
import socket

import pytest
from aiohttp.abc import ResolveResult

from app.telegram_api_session import TelegramApiFallbackResolver


def _resolved_host(host: str, address: str) -> ResolveResult:
    return ResolveResult(
        hostname=host,
        host=address,
        port=443,
        family=socket.AF_INET,
        proto=socket.IPPROTO_TCP,
        flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
    )


class StubResolver:
    def __init__(
        self,
        *,
        result: list[ResolveResult] | None = None,
        error: OSError | None = None,
    ) -> None:
        self.result = result or []
        self.error = error
        self.closed = False

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        if self.error is not None:
            raise self.error
        return self.result

    async def close(self) -> None:
        self.closed = True


def test_telegram_resolver_keeps_dns_first_and_adds_unique_fallbacks() -> None:
    dns_host = _resolved_host("api.telegram.org", "149.154.166.110")
    duplicate_host = _resolved_host("api.telegram.org", "149.154.167.220")
    delegate = StubResolver(result=[dns_host, duplicate_host])

    async def resolve() -> list[ResolveResult]:
        resolver = TelegramApiFallbackResolver(
            fallback_ips=(
                "149.154.167.220",
                "149.154.167.221",
                "2001:67c:4e8:f004::9",
            ),
            resolver=delegate,  # type: ignore[arg-type]
        )
        return await resolver.resolve(
            "API.TELEGRAM.ORG.",
            443,
            socket.AF_UNSPEC,
        )

    result = asyncio.run(resolve())

    assert [item["host"] for item in result] == [
        "149.154.166.110",
        "149.154.167.220",
        "149.154.167.221",
        "2001:67c:4e8:f004::9",
    ]
    assert result[2]["hostname"] == "API.TELEGRAM.ORG."
    assert result[2]["family"] == socket.AF_INET
    assert result[3]["family"] == socket.AF_INET6


def test_telegram_resolver_can_use_fallback_when_dns_fails() -> None:
    delegate = StubResolver(error=OSError("DNS unavailable"))

    async def resolve() -> list[ResolveResult]:
        resolver = TelegramApiFallbackResolver(
            fallback_ips=("149.154.167.220",),
            resolver=delegate,  # type: ignore[arg-type]
        )
        return await resolver.resolve("api.telegram.org", 443, socket.AF_INET)

    result = asyncio.run(resolve())

    assert [item["host"] for item in result] == ["149.154.167.220"]


def test_telegram_resolver_does_not_change_other_hosts_or_hide_dns_errors() -> None:
    dns_host = _resolved_host("example.com", "93.184.216.34")

    async def resolve_success() -> list[ResolveResult]:
        resolver = TelegramApiFallbackResolver(
            fallback_ips=("149.154.167.220",),
            resolver=StubResolver(result=[dns_host]),  # type: ignore[arg-type]
        )
        return await resolver.resolve("example.com", 443, socket.AF_INET)

    async def resolve_failure() -> None:
        resolver = TelegramApiFallbackResolver(
            fallback_ips=("149.154.167.220",),
            resolver=StubResolver(  # type: ignore[arg-type]
                error=OSError("DNS unavailable")
            ),
        )
        await resolver.resolve("example.com", 443, socket.AF_INET)

    assert asyncio.run(resolve_success()) == [dns_host]
    with pytest.raises(OSError, match="DNS unavailable"):
        asyncio.run(resolve_failure())


def test_telegram_resolver_closes_its_delegate() -> None:
    delegate = StubResolver()

    async def close() -> None:
        resolver = TelegramApiFallbackResolver(
            fallback_ips=("149.154.167.220",),
            resolver=delegate,  # type: ignore[arg-type]
        )
        await resolver.close()

    asyncio.run(close())

    assert delegate.closed is True
