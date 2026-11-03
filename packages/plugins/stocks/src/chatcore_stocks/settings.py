"""Environment settings for chatcore_stocks."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StocksSettings(BaseSettings):
    """Base URL for the read-only stock analysis service."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)
    api_base: str = Field(
        default="https://api.stockinvesttoday.com/api", validation_alias="STOCKS_API_BASE"
    )
