"""Environment settings for the optional voice narration plugin."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Narration languages and their ISO 639-1 codes; every one is supported by the allowed models.
LANGUAGES: dict[str, str] = {
    "English": "en",
    "Mandarin Chinese": "zh",
    "Japanese": "ja",
    "Korean": "ko",
    "Malay": "ms",
    "Tamil": "ta",
    "Indonesian": "id",
    "Hindi": "hi",
    "French": "fr",
    "German": "de",
    "Spanish": "es",
}

# Allowed models: (credits charged per character, whether the model accepts ``language_code``).
# Multipliers are conservative (1.0) until live qualification records the real charge.
MODELS: dict[str, tuple[float, bool]] = {
    "eleven_multilingual_v2": (1.0, False),
    "eleven_flash_v2_5": (1.0, True),
    "eleven_turbo_v2_5": (1.0, True),
}

_VOICE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}", flags=re.ASCII)


def parse_voices(value: str) -> dict[str, str]:
    """Parse ``Language=voice_id`` pairs separated by commas."""
    voices: dict[str, str] = {}
    if not value.strip():
        return voices
    for entry in value.split(","):
        language, sep, voice = entry.partition("=")
        language, voice = language.strip(), voice.strip()
        if not sep or language not in LANGUAGES or not _VOICE_ID.fullmatch(voice):
            raise ValueError("Invalid voice narration configuration")
        if language in voices:
            raise ValueError("Duplicate voice narration language")
        voices[language] = voice
    return voices


class VoiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=None, extra="ignore")

    VOICE_TTS_ENABLED: bool = False
    VOICE_ELEVENLABS_API_KEY: SecretStr = SecretStr("")
    VOICE_TTS_VOICES: str = ""
    VOICE_TTS_MODEL: str = "eleven_multilingual_v2"
    VOICE_TTS_MAX_CHARS_PER_REQUEST: int = Field(default=3000, ge=1, le=5000)
    VOICE_TTS_MAX_CHARS_PER_USER_HOUR: int = Field(default=10_000, ge=0, le=1_000_000)
    VOICE_TTS_MAX_CHARS_PER_DAY: int = Field(default=50_000, ge=0, le=10_000_000)
    VOICE_TTS_USAGE_CHECK: Literal["required", "advisory", "off"] = "required"
    VOICE_TTS_USAGE_CACHE_SECONDS: int = Field(default=60, ge=0, le=600)
    VOICE_TTS_PROVIDER_RESERVE_CHARS: int = Field(default=2000, ge=0, le=1_000_000)
    VOICE_TTS_ALLOW_OVERAGE: bool = False

    @field_validator("VOICE_TTS_VOICES")
    @classmethod
    def validate_voices(cls, value: str) -> str:
        parse_voices(value)
        return value

    @field_validator("VOICE_TTS_MODEL")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if value not in MODELS:
            raise ValueError("Unsupported voice narration model")
        return value

    @property
    def voices(self) -> dict[str, str]:
        return parse_voices(self.VOICE_TTS_VOICES)
