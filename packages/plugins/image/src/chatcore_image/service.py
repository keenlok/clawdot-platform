"""Bounded OpenAI image generation with process-local daily reservations."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import openai

from chatcore_image.settings import SIZES, ImageSettings

log = logging.getLogger("chatcore_image")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_ACTIVE = 2


class ImageError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ImageService:
    def __init__(
        self,
        settings: ImageSettings,
        *,
        client=None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self._client = client
        self._owns_client = False
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._day = self._clock().astimezone(UTC).date()
        self._total = 0
        self._users: dict[str, int] = defaultdict(int)
        self._active = 0

    @property
    def available(self) -> bool:
        return bool(
            self.settings.IMAGE_ENABLED
            and self.settings.IMAGE_OPENAI_API_KEY.get_secret_value()
            and self.settings.IMAGE_MAX_PER_DAY > 0
        )

    def _get_client(self):
        if self._client is None:
            timeout = httpx.Timeout(120, connect=5)
            http_client = httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=timeout
            )
            self._client = openai.AsyncOpenAI(
                api_key=self.settings.IMAGE_OPENAI_API_KEY.get_secret_value(),
                base_url=self.settings.IMAGE_OPENAI_BASE_URL,
                timeout=timeout,
                max_retries=0,
                http_client=http_client,
            )
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.close()

    def _roll_day(self) -> None:
        today = self._clock().astimezone(UTC).date()
        if today != self._day:
            self._day, self._total = today, 0
            self._users.clear()

    async def report(self) -> dict[str, object]:
        async with self._lock:
            self._roll_day()
            return {
                "total": self._total,
                "daily_limit": self.settings.IMAGE_MAX_PER_DAY,
                "model": self.settings.IMAGE_MODEL,
                "quality": self.settings.IMAGE_QUALITY,
            }

    async def generate(self, prompt: str, size: str, *, user_key: str) -> bytes:
        async with self._lock:
            self._roll_day()
            if self._users[user_key] >= self.settings.IMAGE_MAX_PER_USER_DAY or (
                self._total >= self.settings.IMAGE_MAX_PER_DAY
            ):
                raise ImageError("quota")
            if self._active >= _MAX_ACTIVE:
                raise ImageError("busy")
            self._users[user_key] += 1
            self._total += 1
            self._active += 1
        started = time.monotonic()
        try:
            try:
                async with asyncio.timeout(130):
                    response = await self._get_client().images.generate(
                        model=self.settings.IMAGE_MODEL,
                        prompt=prompt,
                        size=SIZES[size],
                        quality=self.settings.IMAGE_QUALITY,
                        n=1,
                        output_format="png",
                        moderation=self.settings.IMAGE_MODERATION,
                    )
            except openai.BadRequestError as exc:
                body = getattr(exc, "body", None)
                code = getattr(exc, "code", None)
                code_text = str(code or "").casefold()
                body_text = str(body or "").casefold()
                if (
                    "moderation" in code_text
                    or "moderation" in body_text
                    or code == "content_policy_violation"
                ):
                    log.warning(
                        "image generate failed code=%s",
                        getattr(exc, "status_code", None) or type(exc).__name__,
                    )
                    raise ImageError("refused") from None
                log.warning(
                    "image generate failed code=%s",
                    getattr(exc, "status_code", None) or type(exc).__name__,
                )
                raise ImageError("unavailable") from None
            except (openai.APIError, TimeoutError, httpx.HTTPError) as exc:
                log.warning(
                    "image generate failed code=%s",
                    getattr(exc, "status_code", None) or type(exc).__name__,
                )
                raise ImageError("unavailable") from None
            try:
                encoded = response.data[0].b64_json
                data = base64.b64decode(encoded, validate=True)
            except (AttributeError, IndexError, TypeError, ValueError, binascii.Error):
                raise ImageError("invalid_output") from None
            if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > MAX_IMAGE_BYTES:
                raise ImageError("invalid_output")
            log.info(
                "image generated model=%s size=%s bytes=%s ms=%s",
                self.settings.IMAGE_MODEL,
                size,
                len(data),
                int((time.monotonic() - started) * 1000),
            )
            return data
        finally:
            async with self._lock:
                self._active -= 1
