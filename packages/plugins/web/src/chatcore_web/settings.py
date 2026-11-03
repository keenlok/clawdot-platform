"""Environment settings for chatcore_web."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebSettings(BaseSettings):
    """Limits and credentials for web tools."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)
    tavily_api_key: SecretStr = Field(validation_alias="TAVILY_API_KEY")
    max_results: int = Field(default=5, validation_alias="WEB_MAX_RESULTS")
    fetch_max_bytes: int = Field(default=5_000_000, validation_alias="WEB_FETCH_MAX_BYTES")
    fetch_timeout_s: float = Field(default=10, validation_alias="WEB_FETCH_TIMEOUT_S")
    fetch_max_chars: int = Field(default=50_000, validation_alias="WEB_FETCH_MAX_CHARS")
    max_fetches_per_turn: int = Field(default=3, validation_alias="WEB_MAX_FETCHES_PER_TURN")
