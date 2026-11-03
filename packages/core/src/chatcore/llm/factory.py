"""Provider configuration stays in core so apps only choose settings."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from chatcore.llm.anthropic_provider import AnthropicProvider
from chatcore.llm.base import Provider
from chatcore.llm.openai_compat import OpenAICompatProvider


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=None, extra="ignore")

    LLM_PROVIDER: Literal["openai_compat", "anthropic"] = "openai_compat"
    LLM_BASE_URL: str | None = None
    LLM_MODEL: str = ""
    LLM_API_KEY: SecretStr | None = None
    LLM_MAX_TOKENS: int = 2048
    LLM_TIMEOUT_S: float = 60
    LLM_VISION: bool = False
    LLM_EFFORT: Literal["low", "medium", "high"] = "low"
    LLM_ANTHROPIC_FALLBACKS: bool = True
    # Per-chat /model override (plans/model-switch.md). Empty choices turn it off.
    LLM_MODEL_CHOICES: str = ""
    LLM_MODEL_OVERRIDE_MINUTES: int = Field(default=30, ge=1, le=240)
    LLM_MODEL_OVERRIDE_MAX_MINUTES: int = Field(default=60, ge=1, le=180)
    LLM_MODEL_SWITCH_COOLDOWN_S: int = Field(default=300, ge=0, le=3600)
    LLM_MODEL_SWITCH: Literal["anyone", "admins"] = "anyone"


class VisionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=None, extra="ignore")

    VISION_PROVIDER: Literal["openai_compat", "anthropic"] | None = None
    VISION_BASE_URL: str | None = None
    VISION_MODEL: str = ""
    VISION_API_KEY: SecretStr | None = None
    VISION_MAX_TOKENS: int = 1500
    VISION_TIMEOUT_S: float = 45
    VISION_DESCRIBE: bool = False
    VISION_RECEIPT_MODEL: str = ""
    VISION_DESCRIBE_PER_USER_PER_DAY: int = Field(default=30, ge=0, le=500)


def build_vision_provider(settings: VisionSettings) -> Provider | None:
    """Build a separate image provider through the ordinary provider construction path."""
    if settings.VISION_PROVIDER is None:
        return None
    if settings.VISION_API_KEY is None:
        raise ValueError("VISION_API_KEY is required when VISION_PROVIDER is set")
    if not settings.VISION_MODEL:
        raise ValueError("VISION_MODEL is required when VISION_PROVIDER is set")
    return build_provider(
        LLMSettings(
            LLM_PROVIDER=settings.VISION_PROVIDER,
            LLM_BASE_URL=settings.VISION_BASE_URL,
            LLM_MODEL=settings.VISION_MODEL,
            LLM_API_KEY=settings.VISION_API_KEY,
            LLM_MAX_TOKENS=settings.VISION_MAX_TOKENS,
            LLM_TIMEOUT_S=settings.VISION_TIMEOUT_S,
            LLM_VISION=True,
        )
    )


def build_provider(settings: LLMSettings) -> Provider:
    """Build the configured official SDK client; missing keys fail at startup."""
    if settings.LLM_API_KEY is None:
        raise ValueError("LLM_API_KEY is required when an LLM provider is enabled")
    if settings.LLM_PROVIDER == "anthropic":
        return AnthropicProvider(
            api_key=settings.LLM_API_KEY.get_secret_value(),
            model=settings.LLM_MODEL or "claude-opus-5-5",
            timeout=settings.LLM_TIMEOUT_S,
            effort=settings.LLM_EFFORT,
            fallbacks=settings.LLM_ANTHROPIC_FALLBACKS,
        )
    return OpenAICompatProvider(
        api_key=settings.LLM_API_KEY.get_secret_value(),
        base_url=settings.LLM_BASE_URL,
        model=settings.LLM_MODEL,
        timeout=settings.LLM_TIMEOUT_S,
    )
