"""Bounded, process-local narration with an ElevenLabs subscription-usage preflight."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
from chatcore import tts

from chatcore_voice.settings import LANGUAGES, MODELS, VoiceSettings

log = logging.getLogger("chatcore_voice")

MAX_AUDIO_BYTES = 8 * 1024 * 1024
OUTPUT_FORMAT = "mp3_44100_64"
_ACTIVE_STATUSES = {"active", "trialing", "free"}
_MAX_USERS = 10_000
_MAX_ACTIVE = 2


class NarrationError(Exception):
    """An opaque safe narration failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class UsageReport:
    """What ``/voice usage`` shows: local counters and the provider's figures, if readable."""

    day_chars: int
    day_limit: int
    provider: tts.SubscriptionUsage | None
    provider_error: str | None


class UsageGuard:
    """Caches the subscription counters and reserves in-flight narration cost against them.

    The counters are account-wide, so ``reserve`` keeps ``VOICE_TTS_PROVIDER_RESERVE_CHARS``
    free for everything else on the account."""

    def __init__(
        self,
        settings: VoiceSettings,
        client: httpx.AsyncClient,
        *,
        monotonic: Callable[[], float],
        fetch: Callable[..., Awaitable[tts.SubscriptionUsage]] = tts.fetch_subscription,
    ):
        self.settings, self._client, self._monotonic, self._fetch = (
            settings,
            client,
            monotonic,
            fetch,
        )
        self._lock = asyncio.Lock()
        self._usage: tts.SubscriptionUsage | None = None
        self._fetched_at = 0.0
        self._pending = 0
        self._spent = 0

    async def _read(self, *, force: bool) -> tts.SubscriptionUsage:
        now = self._monotonic()
        ttl = self.settings.VOICE_TTS_USAGE_CACHE_SECONDS
        if force or self._usage is None or ttl == 0 or now - self._fetched_at >= ttl:
            usage = await self._fetch(
                self._client, key=self.settings.VOICE_ELEVENLABS_API_KEY.get_secret_value()
            )
            self._usage, self._fetched_at, self._spent = usage, now, 0
        return self._usage

    async def reserve(self, cost: int) -> int:
        """Return the cost reserved (``0`` when unchecked) or raise ``NarrationError``."""
        mode = self.settings.VOICE_TTS_USAGE_CHECK
        if mode == "off":
            return 0
        async with self._lock:
            try:
                usage = await self._read(force=False)
            except tts.TTSError as exc:
                log.warning("voice usage check failed code=%s", exc.code)
                if mode == "advisory":
                    return 0
                raise NarrationError("usage_unavailable") from None
            if usage.status not in _ACTIVE_STATUSES:
                log.warning("voice usage refused status=%s", usage.status)
                raise NarrationError("account_inactive")
            if usage.overage_possible and not self.settings.VOICE_TTS_ALLOW_OVERAGE:
                log.warning("voice usage refused: provider overage is enabled")
                raise NarrationError("overage_enabled")
            available = usage.remaining - self._spent - self._pending
            if available - cost < self.settings.VOICE_TTS_PROVIDER_RESERVE_CHARS:
                raise NarrationError("provider_quota")
            self._pending += cost
            return cost

    async def settle(self, reserved: int) -> None:
        """A provider attempt was made: count its reserved cost as spent until the next read."""
        if reserved:
            async with self._lock:
                self._pending -= reserved
                self._spent += reserved

    async def report(self) -> tuple[tts.SubscriptionUsage | None, str | None]:
        async with self._lock:
            try:
                return await self._read(force=True), None
            except tts.TTSError as exc:
                return None, exc.code


class NarrationService:
    def __init__(
        self,
        settings: VoiceSettings,
        *,
        client: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
        fetch_usage: Callable[..., Awaitable[tts.SubscriptionUsage]] = tts.fetch_subscription,
    ):
        self.settings = settings
        self._voices = settings.voices
        self._client = client if client is not None else tts.new_client(60)
        self._owns_client = client is None
        self._monotonic, self._utcnow = monotonic, utcnow
        self.usage = UsageGuard(settings, self._client, monotonic=monotonic, fetch=fetch_usage)
        self._lock = asyncio.Lock()
        self._active = 0
        self._day: date = utcnow().date()
        self._day_chars = 0
        self._users: dict[str, deque[tuple[float, int]]] = {}

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(language for language in LANGUAGES if language in self._voices)

    @property
    def available(self) -> bool:
        return bool(
            self.settings.VOICE_TTS_ENABLED
            and self.settings.VOICE_ELEVENLABS_API_KEY.get_secret_value()
            and self.settings.VOICE_TTS_MAX_CHARS_PER_DAY > 0
            and self.settings.VOICE_TTS_MAX_CHARS_PER_USER_HOUR > 0
            and self._voices
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _roll_day(self) -> None:
        today = self._utcnow().date()
        if today != self._day:
            self._day, self._day_chars = today, 0

    async def report(self) -> UsageReport:
        async with self._lock:
            self._roll_day()
            day_chars = self._day_chars
        provider, error = await self.usage.report()
        return UsageReport(day_chars, self.settings.VOICE_TTS_MAX_CHARS_PER_DAY, provider, error)

    async def _reserve_local(self, user_key: str, cost: int) -> tuple[float, int]:
        async with self._lock:
            now = self._monotonic()
            self._users = {
                user: times
                for user, times in self._users.items()
                if times and now - times[-1][0] < 3600
            }
            self._roll_day()
            if self._active >= _MAX_ACTIVE:
                raise NarrationError("busy")
            times = self._users.get(user_key)
            if times:
                while times and now - times[0][0] >= 3600:
                    times.popleft()
            used = sum(chars for _, chars in times) if times else 0
            if (
                used + cost > self.settings.VOICE_TTS_MAX_CHARS_PER_USER_HOUR
                or self._day_chars + cost > self.settings.VOICE_TTS_MAX_CHARS_PER_DAY
            ):
                raise NarrationError("quota")
            if times is None:
                if len(self._users) >= _MAX_USERS:
                    raise NarrationError("quota")
                times = self._users[user_key] = deque()
            self._active += 1
            self._day_chars += cost
            entry = (now, cost)
            times.append(entry)
            return entry

    async def _refund_local(self, user_key: str, entry: tuple[float, int]) -> None:
        """Undo a local reservation when no provider attempt was made."""
        async with self._lock:
            self._active -= 1
            if self._day == self._utcnow().date():
                self._day_chars = max(0, self._day_chars - entry[1])
            times = self._users.get(user_key)
            if times and entry in times:
                times.remove(entry)

    async def narrate(self, text: object, language: object, *, user_key: str) -> bytes:
        limit = self.settings.VOICE_TTS_MAX_CHARS_PER_REQUEST
        if not tts.valid_speech_text(text, max_chars=limit):
            raise NarrationError("invalid_text")
        assert isinstance(text, str)
        if not isinstance(language, str) or language not in self._voices:
            raise NarrationError("invalid_language")
        if not isinstance(user_key, str) or not user_key:
            raise NarrationError("invalid_user")
        if not self.available:
            raise NarrationError("unavailable")
        model = self.settings.model_for(language)
        multiplier, accepts_language = MODELS[model]
        cost = len(text)
        entry = await self._reserve_local(user_key, cost)
        try:
            reserved = await self.usage.reserve(math.ceil(cost * multiplier))
        except BaseException:
            await self._refund_local(user_key, entry)
            raise
        try:
            return await asyncio.wait_for(
                tts.synthesize(
                    self._client,
                    key=self.settings.VOICE_ELEVENLABS_API_KEY.get_secret_value(),
                    text=text,
                    voice=self._voices[language],
                    model=model,
                    output_format=OUTPUT_FORMAT,
                    max_bytes=MAX_AUDIO_BYTES,
                    language_code=LANGUAGES[language] if accepts_language else None,
                ),
                timeout=70,
            )
        except tts.TTSError as exc:
            if exc.detail is not None:
                log.warning("voice narration request failed error=%s", exc.detail)
            raise NarrationError(exc.code) from None
        except TimeoutError:
            raise NarrationError("timeout") from None
        finally:
            await self.usage.settle(reserved)
            async with self._lock:
                self._active -= 1
