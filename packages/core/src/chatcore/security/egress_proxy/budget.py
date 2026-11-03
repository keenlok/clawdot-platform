from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

__all__ = ["RenderBudget"]


@dataclass(slots=True)
class RenderBudget:
    max_bytes: int = 12 * 1024 * 1024
    max_concurrent_tunnels: int = 24
    max_tunnels: int = 150
    idle_timeout_s: float = 15
    bytes_used: int = 0
    tunnels_opened: int = 0
    too_large: bool = False
    refused_authorities: list[str] = field(default_factory=list)
    connect_refused_authorities: list[str] = field(default_factory=list)
    route_refused_authorities: list[str] = field(default_factory=list)
    _active: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def open_tunnel(self) -> bool:
        async with self._lock:
            if self.too_large:
                return False
            if (
                self.tunnels_opened >= self.max_tunnels
                or self._active >= self.max_concurrent_tunnels
            ):
                self.too_large = True
                return False
            self._active += 1
            self.tunnels_opened += 1
            return True

    async def close_tunnel(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)

    async def charge(self, count: int) -> bool:
        async with self._lock:
            if self.too_large:
                return False
            self.bytes_used += count
            if self.bytes_used > self.max_bytes:
                self.too_large = True
                return False
            return True

    def refuse(self, authority: str, *, source: str = "connect") -> None:
        destination = authority.rsplit("@", 1)[-1]
        if destination.startswith("[") and "]" in destination:
            hostname = destination[1 : destination.index("]")]
        else:
            hostname = destination.split(":", 1)[0]
        hostname = hostname.casefold().rstrip(".")
        self.refused_authorities.append(hostname)
        if source == "route":
            self.route_refused_authorities.append(hostname)
        else:
            self.connect_refused_authorities.append(hostname)
