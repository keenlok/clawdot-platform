from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExtensionsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EXTENSIONS_",
        env_file=None,
        extra="ignore",
        case_sensitive=True,
        populate_by_name=True,
    )

    enabled: bool = Field(default=False, validation_alias="EXTENSIONS_ENABLED")
    worker_socket: str | None = Field(default=None, validation_alias="EXTENSIONS_WORKER_SOCKET")
    worker_token_file: str | None = Field(
        default=None, validation_alias="EXTENSIONS_WORKER_TOKEN_FILE"
    )
    runtime: str = Field(default="runc", validation_alias="EXTENSIONS_RUNTIME")
    runtime_manifest_digest: str | None = Field(
        default=None, validation_alias="EXTENSIONS_RUNTIME_MANIFEST_DIGEST"
    )

    @field_validator("enabled", mode="before")
    @classmethod
    def require_canonical_enabled(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if value == "true":
            return True
        if value == "false":
            return False
        raise ValueError("EXTENSIONS_ENABLED must be lowercase true or false")

    @model_validator(mode="after")
    def require_credentials_when_enabled(self) -> ExtensionsSettings:
        if self.enabled and (not self.worker_socket or not self.worker_token_file):
            raise ValueError("worker_socket and worker_token_file are required")
        if self.runtime not in {"runc", "runsc"}:
            raise ValueError("unsupported extensions runtime")
        if self.runtime == "runsc":
            if not self.runtime_manifest_digest or not re.fullmatch(
                r"[0-9a-f]{64}", self.runtime_manifest_digest
            ):
                raise ValueError("runsc requires EXTENSIONS_RUNTIME_MANIFEST_DIGEST")
        elif self.runtime_manifest_digest is not None:
            raise ValueError("runtime manifest digest is only valid for runsc")
        return self
