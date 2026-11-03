"""Fetch and cache historical FX rates without allowing HTTP details into logs."""

from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx
from chatcore.events import ChatRef
from chatcore.store.base import Cache

logger = logging.getLogger(__name__)
CACHE_TTL_S = 24 * 60 * 60
FRANKFURTER_URL = "https://api.frankfurter.dev/v1"
ER_API_URL = "https://open.er-api.com/v6/latest"


async def get_rate(
    cache: Cache,
    chat: ChatRef,
    spent_on: date,
    currency: str,
    settle_currency: str,
    *,
    override: Decimal | str | int | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[Decimal | None, bool]:
    """Prefer trip overrides, then historical Frankfurter, then approximate current rates."""
    if currency.upper() == settle_currency.upper():
        return Decimal(1), False
    if override is not None:
        return _positive_rate(override), False
    key = (
        f"expense-fx:{chat.key}:{currency.upper()}:{settle_currency.upper()}:{spent_on.isoformat()}"
    )
    cached = await cache.get(key)
    if cached:
        try:
            record = json.loads(cached)
            return _positive_rate(record["rate"]), bool(record["approximate"])
        except (ValueError, KeyError, TypeError, InvalidOperation):
            await cache.delete(key)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=5.0)
    try:
        rate = await _frankfurter(client, spent_on, currency, settle_currency)
        approximate = False
        if rate is None:
            rate = await _open_er(client, currency, settle_currency)
            approximate = rate is not None
        if rate is not None:
            await cache.set(
                key,
                json.dumps({"rate": str(rate), "approximate": approximate}).encode(),
                ttl_s=CACHE_TTL_S,
            )
        return rate, approximate
    finally:
        if owns_client:
            await client.aclose()


async def _frankfurter(
    client: httpx.AsyncClient, spent_on: date, base: str, target: str
) -> Decimal | None:
    try:
        response = await client.get(
            f"{FRANKFURTER_URL}/{spent_on.isoformat()}",
            params={"base": base.upper(), "symbols": target.upper()},
        )
        response.raise_for_status()
        body = json.loads(response.text, parse_float=Decimal)
        return _positive_rate(body["rates"][target.upper()])
    except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        logger.info("FX Frankfurter unavailable error_class=%s", type(exc).__name__)
        return None


async def _open_er(client: httpx.AsyncClient, base: str, target: str) -> Decimal | None:
    try:
        response = await client.get(f"{ER_API_URL}/{base.upper()}")
        response.raise_for_status()
        body = json.loads(response.text, parse_float=Decimal)
        return _positive_rate(body["rates"][target.upper()])
    except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        logger.info("FX fallback unavailable error_class=%s", type(exc).__name__)
        return None


def _positive_rate(value: Decimal | str | int) -> Decimal:
    rate = Decimal(str(value))
    if not rate.is_finite() or rate <= 0:
        raise ValueError("FX rate must be positive and finite.")
    return rate
