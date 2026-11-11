"""Environment settings for summary cards."""

from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CardsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=None, extra="ignore")

    CARDS_ENABLED: bool = False
    CARDS_RENDER_URL: str = ""
    CARDS_THEME: str = "light"
    CARDS_TIMEOUT_SECONDS: int = Field(default=20, ge=1, le=60)
    CARDS_MAX_TEXT_CHARS: int = Field(default=8000, ge=1000, le=12000)
    CARDS_MAX_PER_USER_HOUR: int = Field(default=20, ge=0, le=1000)
    CARDS_FOOTER: str = Field(default="", max_length=60)

    @field_validator("CARDS_RENDER_URL")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Cards renderer URL must be http(s) without userinfo")
        return value

    @field_validator("CARDS_THEME")
    @classmethod
    def validate_theme(cls, value: str) -> str:
        if value not in {"light", "dark"}:
            raise ValueError("Cards theme must be light or dark")
        return value

    @field_validator("CARDS_FOOTER")
    @classmethod
    def validate_footer(cls, value: str) -> str:
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
            raise ValueError("Cards footer cannot contain control characters")
        return value
