"""Validated global ceilings for scheduled jobs."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class JobsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOBS_", env_file=None, extra="ignore")

    dm_enabled: bool = False
    max_jobs_per_user: StrictInt = Field(default=10, ge=1, le=1000)
    max_jobs_per_chat: StrictInt = Field(default=100, ge=1, le=1000)
    max_jobs_global: StrictInt = Field(default=1000, ge=1, le=100_000)
    minimum_watch_interval_seconds: StrictInt = Field(default=300, ge=1, le=604800)
    maximum_watch_interval_seconds: StrictInt = Field(default=604800, ge=1, le=604800)
    minimum_rendered_watch_interval_seconds: StrictInt = Field(default=3600, ge=60, le=604800)
    max_rendered_watches_global: StrictInt = Field(default=20, ge=0, le=1000)
    rendered_concurrency: StrictInt = Field(default=1, ge=1, le=4)
    rendered_fetch_timeout_seconds: StrictInt = Field(default=35, ge=1, le=80)
    reminder_horizon_days: StrictInt = Field(default=366, ge=1, le=366)
    reminder_max_chars: StrictInt = Field(default=1000, ge=1, le=1000)
    url_max_chars: StrictInt = Field(default=2048, ge=1, le=2048)
    fetch_max_bytes: StrictInt = Field(default=262144, ge=1, le=262144)
    fetch_timeout_seconds: StrictInt = Field(default=10, ge=1, le=10)
    extracted_max_chars: StrictInt = Field(default=20000, ge=1, le=20000)
    worker_concurrency: StrictInt = Field(default=2, ge=1, le=32)
    poll_seconds: StrictInt = Field(default=1, ge=1, le=60)
    execution_deadline_seconds: StrictInt = Field(default=35, ge=1, le=80)
    send_deadline_seconds: StrictInt = Field(default=15, ge=1, le=35)
    lease_seconds: StrictInt = Field(default=60, ge=1, le=120)
    retention_days: StrictInt = Field(default=30, ge=1, le=3650)
    restore_window_seconds: StrictInt = Field(default=86400, ge=1)

    @field_validator(
        "max_jobs_per_user",
        "max_jobs_per_chat",
        "max_jobs_global",
        "minimum_watch_interval_seconds",
        "maximum_watch_interval_seconds",
        "minimum_rendered_watch_interval_seconds",
        "max_rendered_watches_global",
        "rendered_concurrency",
        "rendered_fetch_timeout_seconds",
        "reminder_horizon_days",
        "reminder_max_chars",
        "url_max_chars",
        "fetch_max_bytes",
        "fetch_timeout_seconds",
        "extracted_max_chars",
        "worker_concurrency",
        "poll_seconds",
        "execution_deadline_seconds",
        "send_deadline_seconds",
        "lease_seconds",
        "retention_days",
        "restore_window_seconds",
        mode="before",
    )
    @classmethod
    def _strict_integer_or_env_string(cls, value: object) -> object:
        if type(value) is bool:
            raise ValueError("boolean is not an integer setting")
        if isinstance(value, str) and value.isdecimal():
            return int(value)
        return value

    @field_validator("dm_enabled", mode="before")
    @classmethod
    def _parse_bool(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return value

    @model_validator(mode="after")
    def _related_bounds(self) -> Self:
        if self.minimum_watch_interval_seconds > self.maximum_watch_interval_seconds:
            raise ValueError("minimum interval exceeds maximum")
        if self.lease_seconds <= self.execution_deadline_seconds + self.send_deadline_seconds:
            raise ValueError("lease must exceed execution and send deadlines")
        return self

    @property
    def effective_rendered_fetch_timeout_seconds(self) -> int:
        return min(self.rendered_fetch_timeout_seconds, self.execution_deadline_seconds)


class GroupLimits(BaseModel):
    """Limits a group admin may reduce; no tenant may raise process ceilings."""

    max_jobs_per_user: StrictInt = Field(ge=1, le=100_000)
    max_jobs_per_chat: StrictInt = Field(ge=1, le=100_000)
    minimum_interval_seconds: StrictInt = Field(ge=1, le=604800)

    @field_validator(
        "max_jobs_per_user", "max_jobs_per_chat", "minimum_interval_seconds", mode="before"
    )
    @classmethod
    def _strict_integer(cls, value: object) -> object:
        if type(value) is bool:
            raise ValueError("boolean is not an integer limit")
        if isinstance(value, str) and value.isdecimal():
            return int(value)
        return value

    @model_validator(mode="after")
    def _valid_interval_bounds(self) -> GroupLimits:
        if self.minimum_interval_seconds <= 0:
            raise ValueError("minimum interval must be positive")
        return self

    @classmethod
    def from_values(
        cls,
        ceilings: JobsSettings,
        *,
        max_jobs_per_user: int | None = None,
        max_jobs_per_chat: int | None = None,
        minimum_interval_seconds: int | None = None,
    ) -> GroupLimits:
        return cls(
            max_jobs_per_user=(
                ceilings.max_jobs_per_user if max_jobs_per_user is None else max_jobs_per_user
            ),
            max_jobs_per_chat=(
                ceilings.max_jobs_per_chat if max_jobs_per_chat is None else max_jobs_per_chat
            ),
            minimum_interval_seconds=(
                ceilings.minimum_watch_interval_seconds
                if minimum_interval_seconds is None
                else minimum_interval_seconds
            ),
        )._validate_ceilings(ceilings)

    def _validate_ceilings(self, ceilings: JobsSettings) -> GroupLimits:
        if self.max_jobs_per_user > ceilings.max_jobs_per_user:
            raise ValueError("user limit exceeds global ceiling")
        if self.max_jobs_per_chat > ceilings.max_jobs_per_chat:
            raise ValueError("chat limit exceeds global ceiling")
        if self.minimum_interval_seconds < ceilings.minimum_watch_interval_seconds:
            raise ValueError("minimum interval is below global floor")
        return self
