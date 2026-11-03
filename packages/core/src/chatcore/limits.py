"""Per-user, per-chat and daily-token caps use expiring cache keys."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

from chatcore.events import ChatRef, UserRef
from chatcore.store.base import Cache, Database

log = logging.getLogger(__name__)

DAILY_BUDGET_MIN = 100_000
DAILY_BUDGET_MAX = 100_000_000
EXTRA_ALLOWANCE_MIN = 10_000
EXTRA_ALLOWANCE_MAX = 100_000_000


@dataclass(frozen=True, slots=True)
class BudgetOverride:
    daily_tokens: int | None
    extra_tokens: int | None
    extra_day: date | None
    set_by: str | None
    set_at: datetime | None


class BudgetUsageStore(Protocol):
    async def load(self, day: date) -> int | None: ...

    async def save(self, day: date, tokens: int) -> None: ...


class PgBudgetUsage:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def load(self, day: date) -> int | None:
        async with self.db.transaction() as tx:
            value = await tx.fetchval("SELECT tokens FROM budget_usage WHERE day=$1", day)
            return int(value) if value is not None else None

    async def save(self, day: date, tokens: int) -> None:
        async with self.db.transaction() as tx:
            await tx.execute(
                """INSERT INTO budget_usage(day, tokens) VALUES ($1, $2)
                   ON CONFLICT (day) DO UPDATE SET
                   tokens=GREATEST(budget_usage.tokens, EXCLUDED.tokens), updated_at=now()""",
                day,
                tokens,
            )


class LimitsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=None, extra="ignore")

    RATE_USER_PER_HOUR: int = 20
    RATE_CHAT_PER_DAY: int = 150
    DAILY_TOKEN_BUDGET: int = 2_000_000
    BUDGET_TZ: str = "Asia/Singapore"


class Limits:
    _MIRROR_TIMEOUT_S = 2.0

    def __init__(
        self,
        cache: Cache,
        settings: LimitsSettings | None = None,
        *,
        user_limit: Callable[[UserRef], int | None] | None = None,
        budget_override: Callable[[], BudgetOverride | None] | None = None,
        usage_store: BudgetUsageStore | None = None,
    ) -> None:
        self.cache = cache
        self.settings = settings or LimitsSettings()
        self._user_limit = user_limit
        self._budget_override = budget_override
        self._usage_store = usage_store

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.BUDGET_TZ))

    def _day(self, now: datetime | None = None) -> str:
        return self.day(now).isoformat()

    def day(self, now: datetime | None = None) -> date:
        return (
            (now or datetime.now(ZoneInfo(self.settings.BUDGET_TZ)))
            .astimezone(ZoneInfo(self.settings.BUDGET_TZ))
            .date()
        )

    def _override(self) -> BudgetOverride | None:
        if self._budget_override is None:
            return None
        try:
            value = self._budget_override()
            if value is not None and not isinstance(value, BudgetOverride):
                raise TypeError("invalid budget override")
            return value
        except Exception as exc:
            log.warning("budget override unavailable error=%s", type(exc).__name__)
            return None

    def base_budget(self) -> int:
        default = self.default_budget()
        override = self._override()
        if override is None or override.daily_tokens is None:
            return default
        if (
            type(override.daily_tokens) is not int
            or not DAILY_BUDGET_MIN <= override.daily_tokens <= DAILY_BUDGET_MAX
        ):
            log.warning("invalid daily budget override ignored error=ValueError")
            return default
        return override.daily_tokens

    def default_budget(self) -> int:
        return self.settings.DAILY_TOKEN_BUDGET

    def extra_today(self, now: datetime | None = None) -> int:
        override = self._override()
        if override is None or override.extra_tokens is None or override.extra_day != self.day(now):
            return 0
        if (
            type(override.extra_tokens) is not int
            or not EXTRA_ALLOWANCE_MIN <= override.extra_tokens <= EXTRA_ALLOWANCE_MAX
        ):
            log.warning("invalid extra budget override ignored error=ValueError")
            return 0
        return override.extra_tokens

    def daily_budget(self, now: datetime | None = None) -> int:
        return self.base_budget() + self.extra_today(now)

    async def budget_exhausted(self, now: datetime | None = None) -> bool:
        instant = now or self._now()
        return await self._budget_used_at(instant) >= self.daily_budget(instant)

    def reset_at(self, now: datetime | None = None) -> datetime:
        zone = ZoneInfo(self.settings.BUDGET_TZ)
        instant = (now or datetime.now(zone)).astimezone(zone)
        tomorrow = instant.date() + timedelta(days=1)
        return datetime.combine(tomorrow, datetime.min.time(), zone)

    def _ttl(self, now: datetime) -> int:
        tomorrow = now.date() + timedelta(days=1)
        return max(
            1,
            int(
                (datetime.combine(tomorrow, datetime.min.time(), now.tzinfo) - now).total_seconds()
            ),
        )

    async def check(self, chat: ChatRef, user: UserRef) -> str | None:
        now = self._now()
        user_key = f"limit:user:{user.key}:{self._day(now)}:{now.hour}"
        chat_key = f"limit:chat:{chat.key}:{self._day(now)}"
        if await self.budget_exhausted(now):
            return "The bot has reached today's token budget. Please try again tomorrow."
        user_count = await self.cache.incr(user_key, ttl_s=3600)
        chat_count = await self.cache.incr(chat_key, ttl_s=86400)
        threshold = self._user_limit(user) if self._user_limit else None
        if user_count > (threshold if threshold is not None else self.settings.RATE_USER_PER_HOUR):
            return "You've reached the hourly request limit. Please try again later."
        if chat_count > self.settings.RATE_CHAT_PER_DAY:
            return "This chat has reached today's request limit. Please try again tomorrow."
        return None

    async def record(self, tokens: int) -> int:
        return await self._record_at(tokens, self._now())

    async def _record_at(self, tokens: int, now: datetime) -> int:
        day = self._day(now)
        key = f"budget:{day}"
        async with self.cache.lock(f"{key}:write", ttl_s=10):
            cached = await self.cache.get(key)
            current = int(cached or b"0")
            if cached is None and self._usage_store is not None:
                try:
                    current = (
                        await asyncio.wait_for(
                            self._usage_store.load(self.day(now)),
                            timeout=self._MIRROR_TIMEOUT_S,
                        )
                        or 0
                    )
                except Exception as exc:
                    log.warning("budget usage mirror failed error=%s", type(exc).__name__)
            current += max(0, tokens)
            cached = await self.cache.get(key)
            if cached is not None:
                current = max(current, int(cached))
            await self.cache.set(key, str(current).encode(), ttl_s=self._ttl(now))
        if self._usage_store is not None:
            try:
                await asyncio.wait_for(
                    self._usage_store.save(self.day(now), current),
                    timeout=self._MIRROR_TIMEOUT_S,
                )
            except Exception as exc:
                log.warning("budget usage mirror failed error=%s", type(exc).__name__)
        return current

    async def budget_used(self) -> int:
        return await self._budget_used_at(self._now())

    async def _budget_used_at(self, now: datetime) -> int:
        key = f"budget:{self._day(now)}"
        cached = await self.cache.get(key)
        if cached is not None:
            return int(cached)
        if self._usage_store is None:
            return 0
        async with self.cache.lock(f"{key}:write", ttl_s=10):
            cached = await self.cache.get(key)
            if cached is not None:
                return int(cached)
            try:
                persisted = (
                    await asyncio.wait_for(
                        self._usage_store.load(self.day(now)),
                        timeout=self._MIRROR_TIMEOUT_S,
                    )
                    or 0
                )
            except Exception as exc:
                log.warning("budget usage mirror failed error=%s", type(exc).__name__)
                return 0
            if persisted > 0:
                cached = await self.cache.get(key)
                restored = max(persisted, int(cached)) if cached is not None else persisted
                if cached is None or int(cached) < restored:
                    await self.cache.set(key, str(restored).encode(), ttl_s=self._ttl(now))
                return restored
            return 0
