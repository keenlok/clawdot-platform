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
    card_max_html_bytes: int = Field(default=262_144, alias="CARD_MAX_HTML_BYTES", ge=1024)
    card_timeout_s: float = Field(default=15, alias="CARD_TIMEOUT_S", gt=0, le=60)
    card_max_pages: int = Field(default=4, alias="CARD_MAX_PAGES", ge=1, le=10)
    card_page_css_px: int = Field(default=3600, alias="CARD_PAGE_CSS_PX", ge=1, le=3600)
    card_max_png_bytes: int = Field(default=5 * 1024 * 1024, alias="CARD_MAX_PNG_BYTES", ge=1)
