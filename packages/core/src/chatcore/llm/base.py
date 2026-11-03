"""The interface LLM providers implement. Track A adds the Anthropic and OpenAI-compatible ones."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from chatcore.llm.types import Completion, Message, ToolChoice, ToolSpec


class ProviderError(Exception):
    """A provider call failed. Never include the request, prompt or key in the message."""


class Provider(Protocol):
    name: str

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        system: str,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_tokens: int = 2048,
        session_key: str | None = None,
    ) -> Completion:
        """One model call. ``tool_choice="none"`` keeps tool specs declared so native tool syntax
        is parsed instead of leaking as text, while requiring the model not to call a tool.
        ``session_key`` is a stable per-conversation ID (e.g. the namespaced chat
        key); providers that support it use it for routing or caching (OpenCode's
        ``x-opencode-session`` header)."""
