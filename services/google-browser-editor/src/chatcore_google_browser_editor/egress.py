"""Exact CONNECT host policy layered in front of core DNS/IP pinning."""

from __future__ import annotations

from urllib.parse import urlsplit

from chatcore.security.egress_proxy import EgressPolicy
from chatcore.security.net_guard import BlockedURL


class AllowlistedEgressPolicy:
    """Restrict every HTTPS tunnel, including WebSockets, before core address checks."""

    def __init__(self, hosts: frozenset[str], network_policy: EgressPolicy | None = None) -> None:
        self.hosts = frozenset(host.casefold().rstrip(".") for host in hosts)
        self.network_policy = network_policy or EgressPolicy()

    @property
    def overrides(self):
        return self.network_policy.overrides

    async def open_tunnel(self, authority: str):
        if not authority or any(char in authority for char in "/?#\r\n"):
            raise BlockedURL("The destination authority is invalid.")
        try:
            parts = urlsplit(f"https://{authority}")
            host = parts.hostname
            port = parts.port or 443
            overrides = getattr(self.network_policy, "overrides", None)
            test_port_allowed = bool(overrides and port in overrides.allowed_ports)
            if (
                not host
                or parts.username is not None
                or parts.password is not None
                or (port != 443 and not test_port_allowed)
                or parts.path
                or parts.query
            ):
                raise BlockedURL("The destination authority is invalid.")
            normalized = host.rstrip(".").encode("idna").decode("ascii").casefold()
            if normalized not in self.hosts:
                raise BlockedURL("The resource host is not allowed.")
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, BlockedURL):
                raise
            raise BlockedURL("The destination authority is invalid.") from exc
        return await self.network_policy.open_tunnel(authority)
