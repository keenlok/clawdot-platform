"""Environment settings for image generation."""

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MODELS = ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst")
SIZES = {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"}


class ImageSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=None, extra="ignore")

    IMAGE_ENABLED: bool = False
    IMAGE_OPENAI_API_KEY: SecretStr = SecretStr("")
    IMAGE_OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    IMAGE_MODEL: str = "gpt-image-2.5-flare"
    IMAGE_QUALITY: Literal["low", "medium", "high"] = "medium"
    IMAGE_MODERATION: Literal["auto", "low"] = "auto"
    IMAGE_MAX_PROMPT_CHARS: int = Field(default=2000, ge=1, le=8000)
    IMAGE_MAX_PER_USER_DAY: int = Field(default=10, ge=0, le=1000)
    IMAGE_MAX_PER_DAY: int = Field(default=50, ge=0, le=10000)

    @field_validator("IMAGE_OPENAI_BASE_URL")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("Image API base URL must use HTTPS")
        return value

    @field_validator("IMAGE_MODEL")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if value not in MODELS:
            raise ValueError("Unsupported image model")
        return value
