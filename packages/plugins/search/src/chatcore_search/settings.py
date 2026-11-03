"""Environment settings for chatcore_search."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SearchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SEARCH_", extra="ignore")

    window_size: int = Field(default=6, ge=1)
    attachment_max_bytes: int = 20_000_000
    pdf_max_pages: int = 50
    parse_timeout_s: float = 30
    csv_max_bytes: int = Field(default=2_000_000, ge=1, le=20_000_000)
    csv_max_rows: int = Field(default=500, ge=1, le=10_000)
    csv_max_columns: int = Field(default=32, ge=1, le=256)
    csv_max_field_chars: int = Field(default=10_000, ge=1, le=100_000)
