from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RendererSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)

    render_max_chars: int = Field(default=30_000, alias="RENDER_MAX_CHARS", ge=1, le=300_000)
    render_max_bytes: int = Field(default=12 * 1024 * 1024, alias="RENDER_MAX_BYTES", ge=1024)
    render_concurrency: int = Field(default=2, alias="RENDER_CONCURRENCY", ge=1, le=16)
    render_timeout_s: float = Field(default=30, alias="RENDER_TIMEOUT_S", gt=0, le=120)
    render_network_idle_s: float = Field(default=3, alias="RENDER_NETWORK_IDLE_S", gt=0, le=15)
    render_settle_s: float = Field(default=1, alias="RENDER_SETTLE_S", ge=0, le=5)
    render_max_renders_per_browser: int = Field(
        default=50, alias="RENDER_MAX_RENDERS_PER_BROWSER", ge=1, le=500
    )
    browser_no_sandbox: bool = Field(default=False, alias="BROWSER_NO_SANDBOX")
    net_guard_extra_blocked: str = Field(default="", alias="NET_GUARD_EXTRA_BLOCKED")
