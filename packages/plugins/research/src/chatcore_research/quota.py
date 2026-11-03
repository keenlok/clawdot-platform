"""Daily research quotas and the per-person inline execution slot."""

from __future__ import annotations

import asyncio
import secrets
from typing import Protocol

from chatcore.events import ChatRef, UserRef
from chatcore.limits import Limits
from chatcore.store.base import Cache

from chatcore_research.settings import ResearchSettings


class ResearchAccess(Protocol):
    async def can_research(self, chat: ChatRef, user: UserRef) -> bool: ...


class AllowAllResearch:
    async def can_research(self, chat: ChatRef, user: UserRef) -> bool:
        return True


class ResearchQuota:
    def __init__(self, cache: Cache, limits: Limits, settings: ResearchSettings) -> None:
        self.cache, self.limits, self.settings = cache, limits, settings
        self._locks: dict[str, asyncio.Lock] = {}

    async def check_and_count(self, chat: ChatRef, user: UserRef) -> str | None:
        if await self.limits.budget_exhausted():
            return "The bot has reached today's token budget. Please try again tomorrow."
        day = self.limits._day()
        user_count = await self.cache.incr(f"research:user:{user.key}:{day}", ttl_s=86400)
        if user_count > self.settings.per_user_per_day:
            return (
                f"You've used today's research limit ({self.settings.per_user_per_day}). "
                "Try again tomorrow."
            )
        chat_count = await self.cache.incr(f"research:chat:{chat.key}:{day}", ttl_s=86400)
        if chat_count > self.settings.per_chat_per_day:
            return (
                "This chat has used today's research limit "
                f"({self.settings.per_chat_per_day}). Try again tomorrow."
            )
        return None

    async def acquire_inline(self, user: UserRef) -> str | None:
        key = user.key
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cache_key = f"research:active:{key}"
            if await self.cache.get(cache_key) is not None:
                return None
            token = secrets.token_hex(16)
            await self.cache.set(
                cache_key,
                token.encode(),
                ttl_s=int(max(self.settings.inline_timeout_s, self.settings.tool_timeout_s)) + 30,
            )
            return token

    async def release_inline(self, user: UserRef, token: str) -> None:
        key = user.key
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cache_key = f"research:active:{key}"
            if await self.cache.get(cache_key) == token.encode():
                await self.cache.delete(cache_key)
