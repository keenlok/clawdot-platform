"""Logging with secret redaction (§6a).

Logs carry IDs, tool names, counts, latency and error classes, never message text, prompts or full
URLs. As a backstop, every formatted line (tracebacks included) is scrubbed of configured secret
values and of anything that looks like a token.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

_PATTERNS = [
    # Telegram bot token, also inside /bot<token>/ URLs (so no \b before the digits).
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),  # OpenAI / Anthropic / OpenRouter style keys
    re.compile(r"\btvly-[A-Za-z0-9_-]{16,}"),  # Tavily
    re.compile(r"(?i)\b(bearer|apikey|api-key|authorization)\s*[:=]?\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(://[^:/\s]+:)[^@\s]+(@)"),  # password in a DSN / URL
]

# Loggers that would otherwise log full request URLs, which can contain the bot token.
NOISY_LOGGERS = ("httpx", "httpcore", "telegram", "urllib3", "elastic_transport", "asyncio")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups == 2:
            text = pattern.sub(rf"\1{REDACTED}\2", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = [s for s in secrets if s]

    def add_secrets(self, secrets: Iterable[str]) -> None:
        self._secrets.extend(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> RedactingFormatter:
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", secrets)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return formatter
