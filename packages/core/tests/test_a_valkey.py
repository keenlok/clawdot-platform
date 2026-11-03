"""Optional local Valkey coverage; CI and sandboxes skip without a service."""

from __future__ import annotations

import asyncio
import os
import secrets

import pytest
from chatcore.store.valkey import ValkeyCache
from valkey.asyncio import Valkey

pytestmark = pytest.mark.integration


async def test_valkey_cache_integration():
    url = os.environ.get("TEST_VALKEY_URL", "valkey://127.0.0.1:56379/15")
    client = Valkey.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        pytest.skip(f"Valkey not available: {type(exc).__name__}")
    await client.aclose()

    cache = ValkeyCache(url, prefix="test:")
    key = "track-a:" + secrets.token_hex(6)
    try:
        await cache.set(key, b"value", ttl_s=30)
        assert await cache.get(key) == b"value"
        count_key = key + ":count"
        assert await cache.incr(count_key, ttl_s=30) == 1
        first_ttl = await cache._client.pttl(cache._key(count_key))
        assert first_ttl > 0
        await asyncio.sleep(0.1)
        assert await cache.incr(count_key, ttl_s=30) == 2
        second_ttl = await cache._client.pttl(cache._key(count_key))
        assert 0 < second_ttl < first_ttl
        lock_key = key + ":lock"
        async with cache.lock(lock_key, ttl_s=2):
            assert await cache.get(key) == b"value"
            await cache._client.set(cache._key(lock_key), b"replacement", px=2000)
        assert await cache._client.get(cache._key(lock_key)) == b"replacement"
    finally:
        await cache.delete(key)
        await cache.delete(key + ":count")
        await cache.delete(key + ":lock")
        await cache.close()
