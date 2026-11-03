"""Environment settings for the optional tutor plugin."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class TutorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=None, extra="ignore")

    TUTOR_ENABLED: bool = False
