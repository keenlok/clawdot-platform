"""Rate-limited card rendering service."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import UTC, datetime

from chatcore.cards import CardError, CardRenderClient

from chatcore_cards.settings import CardsSettings

log = logging.getLogger("chatcore_cards")


class CardQuotaError(CardError):
    code = "quota"

    def __init__(self) -> None:
        self.detail = ""
        Exception.__init__(self, "quota")
        self.code = "quota"


class CardService:
    def __init__(self, settings: CardsSettings, *, client=None, clock=None) -> None:
        self.settings = settings
        self.client = client or (
            CardRenderClient(settings.CARDS_RENDER_URL, timeout_s=settings.CARDS_TIMEOUT_SECONDS)
            if settings.CARDS_RENDER_URL
            else None
        )
        self.clock = clock or time.time
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return bool(
            self.settings.CARDS_ENABLED
            and self.settings.CARDS_RENDER_URL
            and self.settings.CARDS_MAX_PER_USER_HOUR > 0
            and self.client
        )

    async def render(self, card, *, user_key: str) -> list[bytes]:
        now = self.clock()
        async with self._lock:
            attempts = self._attempts[user_key]
            while attempts and attempts[0] <= now - 3600:
                attempts.popleft()
            if len(attempts) >= self.settings.CARDS_MAX_PER_USER_HOUR:
                raise CardQuotaError()
            attempts.append(now)
        footer = self.settings.CARDS_FOOTER.strip()
        timestamp = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d %H:%M UTC")
        footer = f"{footer} · {timestamp}" if footer else timestamp
        started = time.monotonic()
        try:
            pages = await self.client.render(card, theme=self.settings.CARDS_THEME, footer=footer)
            log.info(
                "card rendered pages=%d bytes=%d ms=%d",
                len(pages),
                sum(map(len, pages)),
                int((time.monotonic() - started) * 1000),
            )
            return pages
        except CardError as exc:
            log.warning("card render failed code=%s", exc.code)
            raise
        except Exception:
            log.warning("card render failed code=unavailable")
            raise CardError("unavailable") from None

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
