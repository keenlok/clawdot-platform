"""Bounded, process-local ElevenLabs speech generation for tutor pronunciation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

import httpx

from chatcore_tutor.audio import MAX_AUDIO_BYTES, valid_mp3, valid_tutor_text
from chatcore_tutor.settings import TutorSettings

log = logging.getLogger("chatcore_tutor")
LANGUAGES = {
    "Cantonese": ("yue", "TUTOR_TTS_CANTONESE_VOICE_ID"),
    "Mandarin Chinese": ("zh", "TUTOR_TTS_MANDARIN_VOICE_ID"),
    "Japanese": ("ja", "TUTOR_TTS_JAPANESE_VOICE_ID"),
}


class SpeechError(Exception):
    """An opaque safe speech failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SpeechService:
    def __init__(
        self,
        settings: TutorSettings,
        *,
        client: httpx.AsyncClient | None = None,
        monotonic=time.monotonic,
        utcnow=lambda: datetime.now(UTC),
        slow_processor: Callable[[bytes], Awaitable[bytes]] | None = None,
        ffmpeg_checker: Callable[[], bool] | None = None,
    ):
        from chatcore_tutor.slow_audio import ffmpeg_available, slow_mp3

        self.settings = settings
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(
                timeout=httpx.Timeout(30, connect=5), follow_redirects=False, trust_env=False
            )
        )
        self._owns_client = client is None
        self._monotonic, self._utcnow = monotonic, utcnow
        self._slow_processor = slow_processor or slow_mp3
        self._ffmpeg_checker = ffmpeg_checker or ffmpeg_available
        self._lock = asyncio.Lock()
        self._active = 0
        self._day: date = utcnow().date()
        self._day_chars = 0
        self._users: dict[str, deque[float]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def synthesize(
        self,
        text: object,
        language: object,
        *,
        user_key: str = "unknown",
        pace: str = "normal",
    ) -> bytes:
        if not valid_tutor_text(text):
            raise SpeechError("invalid_text")
        if not isinstance(language, str) or language not in LANGUAGES:
            raise SpeechError("invalid_language")
        if not isinstance(pace, str) or pace not in {"normal", "slow"}:
            raise SpeechError("invalid_pace")
        if not isinstance(user_key, str) or not user_key:
            raise SpeechError("invalid_user")
        if (
            not self.settings.TUTOR_TTS_ENABLED
            or not self.settings.ELEVENLABS_API_KEY.get_secret_value()
        ):
            raise SpeechError("unavailable")
        lang_code, setting = LANGUAGES[language]
        voice = getattr(self.settings, setting)
        if not voice:
            raise SpeechError("voice_unavailable")
        if self.settings.TUTOR_TTS_MAX_CHARS_PER_DAY <= 0:
            raise SpeechError("quota")
        if pace == "slow":
            try:
                if not self._ffmpeg_checker():
                    raise SpeechError("slow_unavailable")
            except SpeechError:
                raise
            except Exception as exc:
                log.warning("tutor ffmpeg preflight failed error=%s", type(exc).__name__)
                raise SpeechError("slow_unavailable") from None

        async with self._lock:
            now = self._monotonic()
            self._users = {
                user: times
                for user, times in self._users.items()
                if times and now - times[-1] < 3600
            }
            today = self._utcnow().date()
            if today != self._day:
                self._day, self._day_chars = today, 0
            if self._active >= 2:
                raise SpeechError("busy")
            times = self._users.get(user_key)
            if times:
                while times and now - times[0] >= 3600:
                    times.popleft()
            used = len(times) if times else 0
            if (
                used >= 20
                or self._day_chars + len(text) > self.settings.TUTOR_TTS_MAX_CHARS_PER_DAY
            ):
                raise SpeechError("quota")
            if times is None:
                if len(self._users) >= 10_000:
                    raise SpeechError("quota")
                times = self._users[user_key] = deque()
            self._active += 1
            self._day_chars += len(text)
            times.append(now)

        try:
            audio = await asyncio.wait_for(
                self._request(
                    text, voice, lang_code, self.settings.ELEVENLABS_API_KEY.get_secret_value()
                ),
                timeout=35,
            )
            if pace == "slow":
                try:
                    audio = await asyncio.wait_for(self._slow_processor(audio), timeout=10)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    raise SpeechError("processor_timeout") from None
                except Exception as exc:
                    log.warning("tutor speech processing failed error=%s", type(exc).__name__)
                    raise SpeechError("processor") from None
            return audio
        except TimeoutError:
            raise SpeechError("timeout") from None
        finally:
            async with self._lock:
                self._active -= 1

    async def _request(self, text: str, voice: str, language: str, key: str) -> bytes:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
        try:
            async with self._client.stream(
                "POST",
                url,
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": key, "accept": "audio/mpeg"},
                json={"text": text, "model_id": "eleven_v3", "language_code": language},
            ) as response:
                if response.status_code != 200:
                    raise SpeechError("provider")
                if response.headers.get("content-type", "").split(";", 1)[0].lower() not in {
                    "audio/mpeg",
                    "audio/mp3",
                }:
                    raise SpeechError("invalid_audio")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise SpeechError("invalid_audio")
                    chunks.append(chunk)
                audio = b"".join(chunks)
                if not valid_mp3(audio):
                    raise SpeechError("invalid_audio")
                return audio
        except (SpeechError, asyncio.CancelledError):
            raise
        except httpx.TimeoutException:
            raise SpeechError("timeout") from None
        except Exception as exc:
            log.warning("tutor speech request failed error=%s", type(exc).__name__)
            raise SpeechError("provider") from None
