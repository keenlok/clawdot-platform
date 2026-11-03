"""Validated cost and execution limits for research runs."""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ResearchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RESEARCH_", extra="ignore")

    per_user_per_day: int = Field(default=5, ge=1, le=100)
    per_chat_per_day: int = Field(default=20, ge=1, le=1000)
    inline_max_searches: int = Field(default=6, ge=1, le=20)
    inline_max_fetches: int = Field(default=8, ge=0, le=30)
    inline_timeout_s: float = Field(default=90, ge=10, le=300)
    tool_timeout_s: float = Field(default=30, ge=10, le=55)
    inline_token_ceiling: int = Field(default=150000, ge=10000, le=2000000)
    long_max_searches: int = Field(default=12, ge=1, le=40)
    long_max_fetches: int = Field(default=20, ge=0, le=60)
    long_timeout_s: float = Field(default=600, ge=60, le=3600)
    long_token_ceiling: int = Field(default=400000, ge=10000, le=5000000)
    max_attempts: int = Field(default=2, ge=1, le=5)
    page_chars: int = Field(default=12000, ge=1000, le=50000)
    max_tokens: int = Field(default=2048, ge=256, le=8192)
    poll_seconds: float = Field(default=2, ge=0.1, le=60)
    worker_concurrency: int = Field(default=1, ge=1, le=4)
    retention_days: int = Field(default=30, ge=1, le=365)
    topic_max_chars: int = Field(default=500, ge=20, le=2000)

    @model_validator(mode="after")
    def long_run_exceeds_inline(self):
        if self.long_timeout_s <= self.inline_timeout_s:
            raise ValueError("LONG_TIMEOUT_S must exceed INLINE_TIMEOUT_S")
        return self

    @property
    def lease_seconds(self) -> int:
        return int(self.long_timeout_s + 60)
