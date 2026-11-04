"""Environment settings for the optional tutor plugin."""

import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TutorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=None, extra="ignore")

    TUTOR_ENABLED: bool = False
    TUTOR_TTS_ENABLED: bool = False
    ELEVENLABS_API_KEY: SecretStr = SecretStr("")
    TUTOR_TTS_CANTONESE_VOICE_ID: str = ""
    TUTOR_TTS_MANDARIN_VOICE_ID: str = ""
    TUTOR_TTS_JAPANESE_VOICE_ID: str = ""
    TUTOR_TTS_MAX_CHARS_PER_DAY: int = Field(default=5000, ge=0, le=1_000_000)

    @field_validator(
        "TUTOR_TTS_CANTONESE_VOICE_ID",
        "TUTOR_TTS_MANDARIN_VOICE_ID",
        "TUTOR_TTS_JAPANESE_VOICE_ID",
    )
    @classmethod
    def validate_voice_id(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value, flags=re.ASCII):
            raise ValueError("Invalid tutor voice configuration")
        return value
