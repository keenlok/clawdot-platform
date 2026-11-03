"""Core settings. Every secret is a ``SecretStr`` so it never shows up in reprs or tracebacks (§6a).

Plugins define their own settings classes with their own env prefix; apps compose them.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CommaList = Annotated[list[str], NoDecode]


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    bot_name: str = "bot"
    """The bot's handle without ``@``, used for mention and ``/cmd@bot`` parsing."""

    allowed_chats: CommaList = Field(default_factory=list)
    """Namespaced group keys the bot serves, e.g. ``tg:-100123``. Others are left (§6a)."""
    admin_users: CommaList = Field(default_factory=list)
    """Namespaced user keys allowed to run admin commands."""
    owner_users: CommaList = Field(default_factory=list)
    """Immutable recovery and group-setup owners; app wiring handles legacy fallback."""

    pg_dsn: SecretStr | None = None
    valkey_url: SecretStr | None = None
    cache_prefix: str = "sbbot:"

    recent_context_messages: int = 20
    log_level: str = "INFO"

    @field_validator("allowed_chats", "admin_users", "owner_users", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def secret_values(self) -> list[str]:
        """Plain values of every configured secret, for the log redaction filter."""
        return [s.get_secret_value() for s in (self.pg_dsn, self.valkey_url) if s is not None]
