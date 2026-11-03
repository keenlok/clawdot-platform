"""Valkey cache; cache outages are isolated from the rest of the bot."""

from __future__ import annotations

import asyncio
import logging
import secrets
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from valkey.asyncio import Valkey
from valkey.exceptions import ValkeyError

from chatcore.store.base import Cache
from chatcore.store.memory import MemoryCache

log = logging.getLogger(__name__)


class ValkeyCache:
    def __init__(self, url: str, *, prefix: str = "sbbot:") -> None:
        self._client = Valkey.from_url(
            url, decode_responses=False, socket_connect_timeout=2, socket_timeout=2
        )
        self.prefix = prefix

    def _key(self, key: str) -> str:
        return self.prefix + key

    async def get(self, key: str) -> bytes | None:
        value = await self._client.get(self._key(key))
        return value.encode() if isinstance(value, str) else value

    async def set(self, key: str, value: bytes, *, ttl_s: int) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        await self._client.set(self._key(key), value, ex=ttl_s)

    async def incr(self, key: str, *, ttl_s: int) -> int:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        full_key = self._key(key)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(full_key)
            pipe.expire(full_key, ttl_s, nx=True)
            values = await pipe.execute()
        return int(values[0])

    async def delete(self, key: str) -> None:
        await self._client.delete(self._key(key))

    @asynccontextmanager
    async def lock(self, key: str, *, ttl_s: int) -> AsyncIterator[None]:
        token = secrets.token_urlsafe(16)
        full_key = self._key(key)
        deadline = time.monotonic() + ttl_s
        while not await self._client.set(full_key, token, nx=True, px=ttl_s * 1000):
            if time.monotonic() >= deadline:
                raise TimeoutError("cache lock timed out")
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            await self._client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                full_key,
                token,
            )

    async def close(self) -> None:
        await self._client.aclose()


class FallbackCache:
    def __init__(self, primary: Cache, fallback: Cache | None = None) -> None:
        self.primary = primary
        self.fallback = fallback or MemoryCache()
        self._last_warning = 0.0

    def _warn(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_warning >= 60:
            log.warning("Valkey unavailable; using memory cache error=%s", type(exc).__name__)
            self._last_warning = now

    async def close(self) -> None:
        close = getattr(self.primary, "close", None)
        if close is not None:
            await close()

    async def get(self, key: str) -> bytes | None:
        try:
            return await self.primary.get(key)
        except (ValkeyError, OSError, TimeoutError) as exc:
            self._warn(exc)
            return await self.fallback.get(key)

    async def set(self, key: str, value: bytes, *, ttl_s: int) -> None:
        try:
            await self.primary.set(key, value, ttl_s=ttl_s)
        except (ValkeyError, OSError, TimeoutError) as exc:
            self._warn(exc)
            await self.fallback.set(key, value, ttl_s=ttl_s)

    async def incr(self, key: str, *, ttl_s: int) -> int:
        try:
            return await self.primary.incr(key, ttl_s=ttl_s)
        except (ValkeyError, OSError, TimeoutError) as exc:
            self._warn(exc)
            return await self.fallback.incr(key, ttl_s=ttl_s)

    async def delete(self, key: str) -> None:
        try:
            await self.primary.delete(key)
        except (ValkeyError, OSError, TimeoutError) as exc:
            self._warn(exc)
            await self.fallback.delete(key)

    @asynccontextmanager
    async def lock(self, key: str, *, ttl_s: int) -> AsyncIterator[None]:
        try:
            primary_lock = self.primary.lock(key, ttl_s=ttl_s)
            await primary_lock.__aenter__()
        except (ValkeyError, OSError) as exc:
            self._warn(exc)
            async with self.fallback.lock(key, ttl_s=ttl_s):
                yield
            return
        try:
            yield
        except BaseException:
            try:
                await primary_lock.__aexit__(*sys.exc_info())
            except (ValkeyError, OSError, TimeoutError) as exc:
                self._warn(exc)
            raise
        else:
            try:
                await primary_lock.__aexit__(None, None, None)
            except (ValkeyError, OSError, TimeoutError) as exc:
                self._warn(exc)
