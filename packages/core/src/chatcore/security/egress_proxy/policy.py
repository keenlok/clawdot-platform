from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from chatcore.security.net_guard import (
    BlockedURL,
    FetchError,
    NetGuardSettings,
    check_public_ip,
    parse_https_destination,
    resolve_public,
)

__all__ = ["Connector", "EgressPolicy", "PinnedTarget", "PolicyOverrides", "Resolver"]

Resolver = Callable[[str, int], Awaitable[list[str]]]
Connector = Callable[..., Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


@dataclass(frozen=True, slots=True)
class PolicyOverrides:
    """Test-only local endpoint allowances; never populated from process settings."""

    allowed_ports: frozenset[int] = frozenset()
    allowed_loopback: frozenset[str] = frozenset()
    ignore_cert_errors: bool = False


@dataclass(frozen=True, slots=True)
class PinnedTarget:
    host: str
    ip: str
    port: int


class EgressPolicy:
    def __init__(
        self,
        resolver: Resolver = resolve_public,
        connector: Connector = asyncio.open_connection,
        overrides: PolicyOverrides | None = None,
        connect_timeout_s: float = 5.0,
    ) -> None:
        self.resolver = resolver
        self.connector = connector
        self.overrides = overrides
        self.connect_timeout_s = connect_timeout_s

    def _parse(self, authority: str) -> tuple[str, int]:
        if self.overrides is None:
            return parse_https_destination(authority)
        try:
            parts = urlsplit(f"https://{authority}")
            if (
                parts.username is not None
                or parts.password is not None
                or parts.path
                or parts.query
            ):
                raise BlockedURL("The destination authority is invalid.")
            host = parts.hostname
            if not host:
                raise BlockedURL("The destination must include a hostname.")
            port = parts.port or 443
            if port != 443 and port not in self.overrides.allowed_ports:
                raise BlockedURL("The destination port is not allowed.")
            normalized = host.rstrip(".").encode("idna").decode("ascii").lower()
            try:
                literal = ipaddress.ip_address(normalized.strip("[]"))
            except ValueError:
                if "." not in normalized:
                    raise BlockedURL("The destination hostname is invalid.") from None
            else:
                if literal.is_loopback and str(literal) not in self.overrides.allowed_loopback:
                    raise BlockedURL("The destination address is not allowed.")
                if not literal.is_loopback:
                    parse_https_destination(
                        f"[{literal}]:443" if literal.version == 6 else f"{literal}:443"
                    )
            return normalized, port
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, BlockedURL):
                raise
            raise BlockedURL("The destination authority is invalid.") from exc

    async def authorize(self, authority: str) -> PinnedTarget:
        host, port = self._parse(authority)
        addresses = await self.resolver(host, port)
        if not addresses:
            raise FetchError("The hostname returned no addresses.")
        for address in addresses:
            if self.overrides is not None:
                parsed = ipaddress.ip_address(address.split("%", 1)[0])
                if parsed.is_loopback and str(parsed) in self.overrides.allowed_loopback:
                    continue
            check_public_ip(address)
        return PinnedTarget(host, addresses[0], port)

    async def open_tunnel(
        self, authority: str
    ) -> tuple[PinnedTarget, asyncio.StreamReader, asyncio.StreamWriter]:
        host, port = self._parse(authority)
        addresses = await self.resolver(host, port)
        if not addresses:
            raise FetchError("The hostname returned no addresses.")
        for address in addresses:
            if self.overrides is not None:
                parsed = ipaddress.ip_address(address.split("%", 1)[0])
                if parsed.is_loopback and str(parsed) in self.overrides.allowed_loopback:
                    pass
                else:
                    check_public_ip(address)
            else:
                check_public_ip(address)
        last_error: OSError | None = None
        attempts = max(1, min(NetGuardSettings().connect_attempts, 5))
        for address in addresses[:attempts]:
            try:
                reader, writer = await asyncio.wait_for(
                    self.connector(address, port), timeout=self.connect_timeout_s
                )
                return PinnedTarget(host, address, port), reader, writer
            except (OSError, TimeoutError) as exc:
                last_error = exc
        raise OSError("Pinned destination connection failed.") from last_error
