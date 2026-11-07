"""Tools the LLM can call. Defined once as JSON Schema; every provider consumes ``ToolSpec``."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from chatcore.events import Attachment, ChatRef, User
from chatcore.llm.types import ImagePart, ToolSpec, Usage
from chatcore.pending import PendingAction
from chatcore.platform import Capabilities, ChatPlatform
from chatcore.security import injection
from chatcore.store.base import Stores

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def plain_command_form(name: str) -> str:
    """Fallback command spelling when no app supplies the addressed ``/name@bot`` form."""
    return f"/{name}"


@dataclass(slots=True)
class TurnState:
    """Mutable per-turn state shared by every tool call in one agent turn."""

    seen_urls: set[str] = field(default_factory=set)
    """URLs a human posted or ``search_web`` returned this turn: the only ones
    ``fetch_web_page`` may fetch (URL provenance rule, §6a)."""
    read_untrusted: bool = False
    """Set once any tool returned untrusted content; ``remember``/``forget`` then need a click."""
    tool_steps: int = 0
    untrusted_tools: set[str] = field(default_factory=set)
    """Names of tools that returned untrusted content during this turn."""
    downloads: dict[str, bytes] = field(default_factory=dict)
    """Request-only raw attachment bytes, never persisted or sent to the model."""
    fetches: int = 0
    attachments: tuple[Attachment, ...] = ()
    """Image attachments of the current human message and its human reply, for image tools."""
    vision: bool = False
    """Whether the main model can see images this turn."""
    images_remaining: int = 0
    """How many more images tools may attach this turn."""
    image_reader: Any | None = None
    describe_user_key: str = ""
    describe_per_user_per_day: int = 30
    record_usage: Callable[[Usage], Awaitable[None]] | None = None
    limits: Any | None = None
    human_urls: frozenset[str] = frozenset()
    """Current human request and same-topic reply-chain URLs, excluding ambient/search data."""
    tool_attempts: dict[str, int] = field(default_factory=dict)
    """Ephemeral per-turn attempt counts for tools that must be used at most once."""


@dataclass(slots=True)
class ToolContext:
    chat: ChatRef
    user: User
    is_dm: bool
    platform: ChatPlatform
    stores: Stores
    thread_id: str | None = None
    turn: TurnState = field(default_factory=TurnState)
    is_admin: bool = False
    command_form: Callable[[str], str] = plain_command_form
    """How to spell a slash command here (``/name@bot`` in mention-only groups), for errors."""
    source_message_id: str | None = None
    reply_to_message_id: str | None = None

    @property
    def capabilities(self) -> Capabilities:
        return self.platform.capabilities


@dataclass(slots=True)
class ToolResult:
    content: str
    untrusted: bool = False
    """True when ``content`` comes from outside the chat (pages, files, search, API output).
    The agent wraps it in per-turn delimiters and treats it as data, never instructions."""
    is_error: bool = False
    pending_action: PendingAction | None = None
    """Set by write tools instead of writing. The core asks the requester to confirm."""
    persist_content: bool = True
    """Whether to retain this result body in conversation history after the current turn."""
    images: tuple[ImagePart, ...] = ()
    """Request-only image parts for vision turns; never persisted to conversation history."""

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(content=message, is_error=True)


ToolHandler = Callable[..., Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    """Called as ``await handler(ctx, **arguments)``."""
    writes: bool = False
    """A write tool must return a ``pending_action`` and never write directly (§6a)."""
    admin_only: bool = False
    direct: bool = False
    """Opt-in to click-free commit while the turn's untrusted inputs permit it."""
    direct_after: frozenset[str] = frozenset()
    """Untrusted tool outputs that do not block this direct write."""

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"invalid tool name {self.name!r}")
        if self.parameters.get("type") != "object":
            raise ValueError(f"tool {self.name!r}: parameters must be a JSON Schema object")
        if self.direct and not self.writes:
            raise ValueError("direct tools must also be write tools")
        if self.direct_after and not self.direct:
            raise ValueError("direct_after requires a direct tool")

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def available(self, *, is_admin: bool) -> list[Tool]:
        return [t for t in self._tools.values() if is_admin or not t.admin_only]

    def specs(self, *, is_admin: bool) -> list[ToolSpec]:
        return [t.spec for t in self.available(is_admin=is_admin)]

    async def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        if tool is None or (tool.admin_only and not ctx.is_admin):
            return ToolResult.error(f"unknown tool {name!r}")
        unknown = sorted(set(arguments) - set(tool.parameters.get("properties", {})))
        if unknown:
            return ToolResult.error("unknown argument(s): " + ", ".join(unknown))
        missing = sorted(set(tool.parameters.get("required", ())) - set(arguments))
        if missing:
            return ToolResult.error("missing required argument(s): " + ", ".join(missing))
        result = await tool.handler(ctx, **arguments)
        if tool.writes and result.pending_action is None and not result.is_error:
            raise RuntimeError(f"write tool {name!r} returned without a pending action")
        if result.untrusted:
            ctx.turn.read_untrusted = True
            ctx.turn.untrusted_tools.add(name)
            await injection.flag(
                result.content,
                chat_key=ctx.chat.key,
                source=injection.web_source(name, arguments),
                cache=getattr(ctx.stores, "cache", None),
            )
        return result


def direct_write_allowed(tool: Tool, turn: TurnState) -> bool:
    """Whether an opted-in write can commit without a confirmation click this turn."""
    if not tool.direct:
        return False
    if not turn.read_untrusted:
        return True
    return bool(turn.untrusted_tools) and turn.untrusted_tools <= tool.direct_after
