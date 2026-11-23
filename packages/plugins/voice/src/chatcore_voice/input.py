"""Process-local transcription caps and safe wake-word matching."""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime

import httpx
from chatcore import stt, tts
from chatcore.events import Attachment, IncomingMessage, VoiceNote

from chatcore_voice.settings import VoiceSettings

MAX_AUDIO_BYTES = 20 * 1024 * 1024
log = logging.getLogger("chatcore_voice")


def normalized_words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    return [
        "".join(c for c in word if not unicodedata.category(c).startswith("P"))
        for word in text.split()
    ]


def strip_wake_word(text: str, phrases: tuple[str, ...]) -> str | None:
    words = normalized_words(text)
    first = words[:5]
    for phrase in phrases:
        target = normalized_words(phrase)
        count = len(target)
        if not count:
            continue
        for consumed in range(1, min(5, len(first)) + 1):
            if "".join(first[:consumed]) == "".join(target):
                return " ".join(text.split()[consumed:]).strip()
    return None


class TranscriptionService:
    def __init__(self, settings: VoiceSettings, *, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or tts.new_client(30)
        self.owns_client = client is None
        self.daily: dict[tuple[str, str], int] = defaultdict(int)
        self._budget_lock = asyncio.Lock()

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()

    @property
    def available(self) -> bool:
        return self.settings.VOICE_STT_ENABLED and bool(
            self.settings.VOICE_STT_ELEVENLABS_API_KEY.get_secret_value()
        )

    async def _reserve(
        self, day: str, user_key: str, duration: int, *, chat_key: str, wake_mode: bool
    ) -> bool:
        async with self._budget_lock:
            self.daily = defaultdict(
                int, {key: value for key, value in self.daily.items() if key[0] == day}
            )
            user_day, global_day = (day, user_key), (day, "*")
            if (
                self.daily[user_day] + duration > self.settings.VOICE_STT_MAX_SECONDS_PER_USER_DAY
                or self.daily[global_day] + duration > self.settings.VOICE_STT_MAX_SECONDS_PER_DAY
            ):
                return False
            self.daily[user_day] += duration
            self.daily[global_day] += duration
            if wake_mode:
                chat_day = (day, f"chat:{chat_key}")
                if self.daily[chat_day] + duration > self.settings.VOICE_STT_MAX_SECONDS_PER_DAY:
                    self.daily[user_day] -= duration
                    self.daily[global_day] -= duration
                    return False
                self.daily[chat_day] += duration
            return True

    async def transcribe(
        self,
        message: IncomingMessage,
        app,
        attachment: Attachment,
        *,
        wake_words: tuple[str, ...] = (),
        source: str = "self",
    ) -> tuple[IncomingMessage, str | None]:
        if not self.available:
            return message, None
        duration = attachment.duration_s if (attachment.duration_s or 0) > 0 else None
        if duration is None and attachment.size_bytes is not None:
            duration = max(1, round(attachment.size_bytes / 16_000))
        if duration is not None and duration > self.settings.VOICE_STT_MAX_SECONDS_PER_NOTE:
            return message, "long"
        if (
            wake_words
            and duration is not None
            and duration > self.settings.VOICE_STT_WAKE_MAX_SECONDS
        ):
            return message, "wake_long"
        day = datetime.now(UTC).date().isoformat()
        reserved = False
        chat_key = message.chat.key
        if duration is None:
            async with self._budget_lock:
                self.daily = defaultdict(
                    int, {key: value for key, value in self.daily.items() if key[0] == day}
                )
                if (
                    self.daily[(day, message.user.ref.key)]
                    >= self.settings.VOICE_STT_MAX_SECONDS_PER_USER_DAY
                    or self.daily[(day, "*")] >= self.settings.VOICE_STT_MAX_SECONDS_PER_DAY
                    or (
                        wake_words
                        and self.daily[(day, f"chat:{chat_key}")]
                        >= self.settings.VOICE_STT_MAX_SECONDS_PER_DAY
                    )
                ):
                    return message, "stt_quota"
        if duration is not None:
            reserved = await self._reserve(
                day,
                message.user.ref.key,
                duration,
                chat_key=chat_key,
                wake_mode=bool(wake_words),
            )
            if not reserved:
                return message, "stt_quota"
        mode = self.settings.VOICE_STT_USAGE_CHECK
        try:
            data = await app.platform.download(
                attachment, chat=message.chat, max_bytes=MAX_AUDIO_BYTES
            )
        except Exception as exc:
            log.warning("voice download failed error=%s", type(exc).__name__)
            return message, "provider"
        if duration is None:
            duration = max(1, round(len(data) / 16_000))
            if duration > self.settings.VOICE_STT_MAX_SECONDS_PER_NOTE:
                return message, "long"
            if wake_words and duration > self.settings.VOICE_STT_WAKE_MAX_SECONDS:
                return message, "wake_long"
        if not reserved and not await self._reserve(
            day,
            message.user.ref.key,
            duration,
            chat_key=chat_key,
            wake_mode=bool(wake_words),
        ):
            return message, "stt_quota"
        if mode != "off":
            try:
                usage = await tts.fetch_subscription(
                    self.client,
                    key=self.settings.VOICE_STT_ELEVENLABS_API_KEY.get_secret_value(),
                )
                if usage.status not in {"active", "trialing", "free"}:
                    return message, "provider"
            except tts.TTSError as exc:
                log.warning("voice STT usage check failed code=%s", exc.code)
                if mode == "required":
                    return message, "provider"
        try:
            text, language = await stt.transcribe(
                self.client,
                data,
                key=self.settings.VOICE_STT_ELEVENLABS_API_KEY.get_secret_value(),
                model=self.settings.VOICE_STT_MODEL,
                keyterms=list(wake_words),
            )
        except stt.STTError as exc:
            log.warning("voice transcription failed code=%s", exc.status_code or "unknown")
            return message, "provider"
        if not text.strip():
            return message, "unheard"
        matched = False
        if wake_words:
            stripped = strip_wake_word(text, wake_words)
            if stripped is None:
                return message, "wake_miss"
            text, matched = stripped, True
        from dataclasses import replace

        return replace(
            message,
            text=(text if source == "self" else f"{message.text}\n\n[voice transcript: {text}]"),
            mentions_bot=message.mentions_bot or matched,
            voice=VoiceNote(text, language, duration, source, matched),
        ), None
