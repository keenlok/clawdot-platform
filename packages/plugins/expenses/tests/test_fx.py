from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx
from chatcore.events import ChatRef
from chatcore.store.memory import MemoryCache
from chatcore_expenses.fx import ER_API_URL, FRANKFURTER_URL, get_rate


@pytest.mark.asyncio
async def test_override_wins_without_network():
    with respx.mock(assert_all_called=False) as router:
        rate, approximate = await get_rate(
            MemoryCache(), ChatRef("fake", "one"), date(2026, 10, 30), "USD", "SGD", override="1.25"
        )
    assert router.calls.call_count == 0
    assert rate == Decimal("1.25") and approximate is False


@pytest.mark.asyncio
async def test_frankfurter_rate_is_cached_and_not_approximate():
    cache = MemoryCache()
    with respx.mock(assert_all_called=True) as router:
        route = router.get(f"{FRANKFURTER_URL}/2026-10-30?base=USD&symbols=SGD").mock(
            return_value=httpx.Response(200, json={"rates": {"SGD": 1.3}})
        )
        rate, approximate = await get_rate(
            cache, ChatRef("fake", "one"), date(2026, 10, 30), "USD", "SGD"
        )
        cached, cached_approximate = await get_rate(
            cache, ChatRef("fake", "one"), date(2026, 10, 30), "USD", "SGD"
        )
    assert rate == cached == Decimal("1.3")
    assert approximate is cached_approximate is False
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_fallback_is_approximate_and_failure_leaves_none():
    with respx.mock(assert_all_called=True) as router:
        frankfurter = router.get(f"{FRANKFURTER_URL}/2026-10-30?base=USD&symbols=VND").mock(
            return_value=httpx.Response(503)
        )
        fallback = router.get(f"{ER_API_URL}/USD").mock(
            return_value=httpx.Response(200, json={"rates": {"VND": 25000}})
        )
        rate, approximate = await get_rate(
            MemoryCache(), ChatRef("fake", "one"), date(2026, 10, 30), "USD", "VND"
        )
    assert rate == Decimal("25000") and approximate is True
    assert frankfurter.call_count == fallback.call_count == 1
    with respx.mock(assert_all_called=True) as failed_router:
        failed_router.get(f"{FRANKFURTER_URL}/2026-10-30?base=USD&symbols=VND").mock(
            return_value=httpx.Response(503)
        )
        failed_router.get(f"{ER_API_URL}/USD").mock(return_value=httpx.Response(503))
        failed, is_approximate = await get_rate(
            MemoryCache(), ChatRef("fake", "two"), date(2026, 10, 30), "USD", "VND"
        )
    assert failed is None and is_approximate is False
