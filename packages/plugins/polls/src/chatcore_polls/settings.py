"""Validated process limits for native group polls."""

from pydantic import Field, StrictInt, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class PollsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POLLS_", env_file=None, extra="ignore")

    max_options: StrictInt = Field(default=12, ge=2, le=12)
    max_open_per_chat: StrictInt = Field(default=20, ge=1, le=200)
    max_open_per_user: StrictInt = Field(default=5, ge=1, le=50)
    close_horizon_days: StrictInt = Field(default=30, ge=1, le=90)
    min_close_lead_seconds: StrictInt = Field(default=60, ge=5, le=3600)
    scan_seconds: StrictInt = Field(default=30, ge=1, le=300)
    lease_seconds: StrictInt = Field(default=60, ge=10, le=600)
    max_close_failures: StrictInt = Field(default=3, ge=1, le=10)
    retention_days: StrictInt = Field(default=30, ge=1, le=3650)

    @field_validator("*", mode="before")
    @classmethod
    def _strict_integer_or_env_string(cls, value: object) -> object:
        if type(value) is bool:
            raise ValueError("boolean is not an integer setting")
        if isinstance(value, str) and value.isdecimal():
            return int(value)
        return value
