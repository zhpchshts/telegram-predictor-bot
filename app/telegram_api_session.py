from __future__ import annotations

import socket
from ipaddress import IPv4Address, IPv6Address, ip_address

from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver


TELEGRAM_BOT_API_HOST = "api.telegram.org"


class TelegramApiFallbackResolver(AbstractResolver):
    def __init__(
        self,
        *,
        fallback_ips: tuple[str, ...],
        resolver: AbstractResolver | None = None,
    ) -> None:
        self._fallback_addresses = tuple(ip_address(value) for value in fallback_ips)
        self._resolver = resolver or DefaultResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        is_telegram_api = host.rstrip(".").lower() == TELEGRAM_BOT_API_HOST
        try:
            resolved_hosts = await self._resolver.resolve(host, port, family)
        except OSError:
            if not is_telegram_api:
                raise
            resolved_hosts = []

        if not is_telegram_api:
            return resolved_hosts

        combined_hosts = list(resolved_hosts)
        seen_hosts = {item["host"] for item in resolved_hosts}
        for address in self._fallback_addresses:
            address_family = _address_family(address)
            if family not in {socket.AF_UNSPEC, address_family}:
                continue
            normalized_address = str(address)
            if normalized_address in seen_hosts:
                continue
            combined_hosts.append(
                ResolveResult(
                    hostname=host,
                    host=normalized_address,
                    port=port,
                    family=address_family,
                    proto=socket.IPPROTO_TCP,
                    flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                )
            )
            seen_hosts.add(normalized_address)
        return combined_hosts

    async def close(self) -> None:
        await self._resolver.close()


class TelegramApiAiohttpSession(AiohttpSession):
    def __init__(self, *, fallback_ips: tuple[str, ...]) -> None:
        super().__init__()
        self._telegram_api_resolver = TelegramApiFallbackResolver(
            fallback_ips=fallback_ips
        )
        # aiogram does not expose a public resolver argument. Keep the connector
        # customization isolated here so regular Bot API behavior stays intact.
        self._connector_init["resolver"] = self._telegram_api_resolver

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self._telegram_api_resolver.close()


def _address_family(address: IPv4Address | IPv6Address) -> socket.AddressFamily:
    if isinstance(address, IPv4Address):
        return socket.AF_INET
    return socket.AF_INET6
