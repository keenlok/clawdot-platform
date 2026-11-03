"""Process-wide limits for browse tools."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import Field, StrictInt, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrowseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BROWSE_", env_file=None, extra="ignore", populate_by_name=True
    )

    render_url: str | None = Field(default=None, validation_alias="BROWSER_RENDER_URL")
    dm_enabled: bool = False
    request_timeout_s: StrictInt = Field(default=45, ge=1, le=120)
    max_chars: StrictInt = Field(default=30_000, ge=1, le=100_000)
    max_renders_per_turn: StrictInt = Field(default=2, ge=1, le=10)
    max_fetches_per_turn: StrictInt = Field(default=3, ge=1, le=20)
    cache_ttl_s: StrictInt = Field(default=900, ge=1, le=86_400)
    max_response_bytes: StrictInt = Field(default=300_000, ge=1, le=2_000_000)

    @field_validator(
        "request_timeout_s",
        "max_chars",
        "max_renders_per_turn",
        "max_fetches_per_turn",
        "cache_ttl_s",
        "max_response_bytes",
        mode="before",
    )
    @classmethod
    def strict_int(cls, value: object) -> object:
        if type(value) is bool:
            raise ValueError("boolean is not an integer setting")
        if isinstance(value, str) and value.isdecimal():
            return int(value)
        return value

    @field_validator("render_url")
    @classmethod
    def reject_renderer_userinfo(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parts = urlsplit(value)
        except ValueError as exc:
            raise ValueError("BROWSER_RENDER_URL is invalid") from exc
        if parts.username is not None or parts.password is not None:
            raise ValueError("BROWSER_RENDER_URL must not contain userinfo")
        return value

    @field_validator("dm_enabled", mode="before")
    @classmethod
    def parse_bool(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return value
