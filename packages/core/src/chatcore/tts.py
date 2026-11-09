"""Bounded ElevenLabs text-to-speech and subscription-usage requests shared by speech plugins.

Callers own budgets, voices and delivery. This module makes one fixed-origin request at a time
and raises :class:`TTSError` with an opaque code; it never logs keys, text or response bodies.
"""

from __future__ import annotations

import asyncio
import json
import unicodedata
from dataclasses import dataclass

import httpx

ELEVENLABS_ORIGIN = "https://api.elevenlabs.io"
_AUDIO_TYPES = {"audio/mpeg", "audio/mp3"}
_MAX_USAGE_BYTES = 64 * 1024


class TTSError(Exception):
    """An opaque provider failure. ``code`` is safe to log; ``detail`` is an exception class."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code)


def new_client(timeout: float, *, connect: float = 5) -> httpx.AsyncClient:
    """A client with no redirects and no environment proxy inheritance."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=connect), follow_redirects=False, trust_env=False
    )


def valid_speech_text(value: object, *, max_chars: int) -> bool:
    """Nonblank text of 1–``max_chars`` code points without surrogates or control characters
    other than tab and newlines."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= max_chars
        and bool(value.strip())
        and all(
            unicodedata.category(char) != "Cs"
            and (unicodedata.category(char) != "Cc" or char in "\t\n\r")
            for char in value
        )
    )


def valid_mp3(data: bytes) -> bool:
    offset = 0
    if data.startswith(b"ID3"):
        if len(data) < 10:
            return False
        size = data[6:10]
        if any(part & 0x80 for part in size):
            return False
        tag_size = (size[0] << 21) | (size[1] << 14) | (size[2] << 7) | size[3]
        offset = 10 + tag_size + (10 if data[5] & 0x10 else 0)
    return (
        len(data) >= offset + 4
        and data[offset] == 0xFF
        and (data[offset + 1] & 0xE0) == 0xE0
        and (data[offset + 1] & 0x06) != 0
    )


async def synthesize(
    client: httpx.AsyncClient,
    *,
    key: str,
    text: str,
    voice: str,
    model: str,
    output_format: str,
    max_bytes: int,
    language_code: str | None = None,
) -> bytes:
    """Stream one MP3 from the speech endpoint, enforcing ``max_bytes`` while reading."""
    body: dict[str, str] = {"text": text, "model_id": model}
    if language_code is not None:
        body["language_code"] = language_code
    try:
        async with client.stream(
            "POST",
            f"{ELEVENLABS_ORIGIN}/v1/text-to-speech/{voice}",
            params={"output_format": output_format},
            headers={"xi-api-key": key, "accept": "audio/mpeg"},
            json=body,
        ) as response:
            if response.status_code != 200:
                raise TTSError("provider")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in _AUDIO_TYPES:
                raise TTSError("invalid_audio")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise TTSError("invalid_audio")
                chunks.append(chunk)
            audio = b"".join(chunks)
            if not valid_mp3(audio):
                raise TTSError("invalid_audio")
            return audio
    except (TTSError, asyncio.CancelledError):
        raise
    except httpx.TimeoutException:
        raise TTSError("timeout") from None
    except Exception as exc:
        raise TTSError("provider", type(exc).__name__) from None


@dataclass(frozen=True, slots=True)
class SubscriptionUsage:
    """The billing-period counters from ``GET /v1/user/subscription``.

    ``max_extension`` is ``None`` when the provider reports ``"unlimited"`` overage."""

    status: str
    character_count: int
    character_limit: int
    next_reset_unix: int | None
    can_extend: bool
    max_extension: int | None

    @property
    def remaining(self) -> int:
        return self.character_limit - self.character_count

    @property
    def overage_possible(self) -> bool:
        return self.can_extend and self.max_extension != 0


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid counter")
    return value


def parse_subscription(payload: object) -> SubscriptionUsage:
    """Parse only the usage fields; invoices and payment details are ignored."""
    if not isinstance(payload, dict):
        raise ValueError("invalid subscription")
    status = payload.get("status")
    can_extend = payload.get("can_extend_character_limit")
    if not isinstance(status, str) or len(status) > 32 or not isinstance(can_extend, bool):
        raise ValueError("invalid subscription")
    extension = payload.get("max_credit_limit_extension")
    if extension == "unlimited":
        max_extension = None
    else:
        max_extension = _count(extension)
    reset = payload.get("next_character_count_reset_unix")
    return SubscriptionUsage(
        status=status,
        character_count=_count(payload.get("character_count")),
        character_limit=_count(payload.get("character_limit")),
        next_reset_unix=None if reset is None else _count(reset),
        can_extend=can_extend,
        max_extension=max_extension,
    )


async def fetch_subscription(
    client: httpx.AsyncClient, *, key: str, request_timeout: float = 5
) -> SubscriptionUsage:
    """Read the account's current billing-period usage. Codes: ``http_<status>``, ``invalid``,
    ``timeout`` or ``provider``."""
    try:
        async with client.stream(
            "GET",
            f"{ELEVENLABS_ORIGIN}/v1/user/subscription",
            headers={"xi-api-key": key, "accept": "application/json"},
            timeout=request_timeout,
        ) as response:
            if response.status_code != 200:
                raise TTSError(f"http_{response.status_code}")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_USAGE_BYTES:
                    raise TTSError("invalid")
                chunks.append(chunk)
        try:
            return parse_subscription(json.loads(b"".join(chunks)))
        except ValueError:
            raise TTSError("invalid") from None
    except (TTSError, asyncio.CancelledError):
        raise
    except httpx.TimeoutException:
        raise TTSError("timeout") from None
    except Exception as exc:
        raise TTSError("provider", type(exc).__name__) from None
