"""Required worker configuration and safe loading of the dedicated editor session."""

from __future__ import annotations

import ipaddress
import json
import re
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .resource_hosts import RESOURCE_HOSTS, allowed_cookie_domain


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOOGLE_BROWSER_WORKER_", extra="ignore")

    bind: str = "0.0.0.0:8080"
    editor_email: str
    owner_email: str
    state_path: Path
    secret_file: Path
    db_path: Path
    job_timeout_seconds: int = Field(default=90, ge=1, le=90)
    terminal_retention_hours: int = Field(default=24, ge=1, le=720)
    tombstone_retention_days: int = Field(default=400, ge=400, le=1460)
    max_page_bytes: int = Field(default=32 * 1024 * 1024, ge=1024 * 1024, le=128 * 1024 * 1024)
    chromium_channel: Literal["chromium", "chrome"] = "chromium"
    chromium_executable_path: Path | None = None

    @field_validator("editor_email")
    @classmethod
    def editor_is_canonical_email(cls, value: str) -> str:
        if value != value.casefold() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("editor account email must be a lowercase email address")
        return value

    @field_validator("owner_email")
    @classmethod
    def owner_is_canonical_email(cls, value: str) -> str:
        if value != value.casefold() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("owner account email must be a lowercase email address")
        return value

    @model_validator(mode="after")
    def refuse_owner_account(self) -> WorkerSettings:
        if self.editor_email.casefold() == self.owner_email.casefold():
            raise ValueError("editor account must be separate from the owner account")
        if self.state_path.resolve() == self.secret_file.resolve():
            raise ValueError("session seed and worker secret must be separate files")
        repository = Path(__file__).resolve().parents[4]
        for path in (self.state_path, self.secret_file, self.db_path):
            resolved = path.resolve()
            if resolved == repository or repository in resolved.parents:
                raise ValueError(
                    "worker state, secret and journal paths must be outside the repository"
                )
        self.bind_address()
        return self

    def bind_address(self) -> tuple[str, int]:
        host, separator, raw_port = self.bind.rpartition(":")
        if not separator or not raw_port.isdecimal():
            raise ValueError("worker bind must be an IP address and port")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        try:
            normalized_host = str(ipaddress.ip_address(host))
            port = int(raw_port)
        except ValueError as exc:
            raise ValueError("worker bind must be an IP address and port") from exc
        if not 1 <= port <= 65535:
            raise ValueError("worker port is invalid")
        return normalized_host, port

    @staticmethod
    def _private_regular_file(path: Path, description: str) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"{description} must be a regular file")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise RuntimeError(f"{description} must have mode 0600 or stricter")
        return path.read_bytes()

    def read_secret(self) -> str:
        raw = self._private_regular_file(self.secret_file, "worker secret")
        secret = raw.decode("utf-8").strip()
        if len(secret) < 32:
            raise RuntimeError("worker secret must contain at least 32 characters")
        return secret

    def read_storage_state(self) -> dict[str, object]:
        raw = self._private_regular_file(self.state_path, "session seed")
        try:
            state = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("session seed is invalid") from exc
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
            raise RuntimeError("session seed has an unsupported format")
        origins = state.get("origins", [])
        if not isinstance(origins, list):
            raise RuntimeError("session seed has an unsupported format")
        for cookie in state["cookies"]:
            if not isinstance(cookie, dict) or not allowed_cookie_domain(
                str(cookie.get("domain", ""))
            ):
                raise RuntimeError("session seed contains a non-Google cookie")
        for origin in origins:
            if not isinstance(origin, dict) or not isinstance(origin.get("origin"), str):
                raise RuntimeError("session seed has an unsupported origin")
            parsed = urlsplit(origin["origin"])
            if parsed.scheme != "https" or parsed.hostname not in RESOURCE_HOSTS:
                raise RuntimeError("session seed contains a non-allowlisted origin")
        return state
