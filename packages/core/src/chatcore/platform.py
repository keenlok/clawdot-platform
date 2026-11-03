"""The interface every chat-app adapter implements (§3d).

The core only talks to chat apps through ``ChatPlatform``. An adapter declares what it can do in
``Capabilities``; the core falls back when something is missing (e.g. reply codes, not buttons).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from chatcore.events import Attachment, ChatRef, Event, MessageRef, UserRef

EventHandler = Callable[[Event], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Capabilities:
    buttons: bool
    edit: bool
    threads: bool
    files: bool
    dm: bool
    max_message_len: int
    markup: Literal["plain", "markdown", "html"] = "plain"
    pin: bool = False


class PlatformError(Exception):
    """An adapter failed to talk to its chat app. Safe to show its class name, never its args."""


@runtime_checkable
class ChatPlatform(Protocol):
    name: str
    """Namespace used in chat and user keys, e.g. ``"tg"`` or ``"console"``."""

    capabilities: Capabilities

    async def start(self, handler: EventHandler) -> None:
        """Receive events and pass each to ``handler`` until ``stop`` is called."""

    async def stop(self) -> None: ...

    async def send(
        self,
        chat: ChatRef,
        text: str,
        *,
        reply_to: MessageRef | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        """Post text using ``capabilities.markup``; plain adapters display it literally.

        The adapter renders supported markup and splits at ``max_message_len``.

        Link previews must be disabled where the platform has them (§6a).
        """

    async def edit(self, message: MessageRef, text: str) -> None:
        """Replace a message's text. Only called when ``capabilities.edit`` is true."""

    async def typing(self, chat: ChatRef, thread_id: str | None = None) -> None: ...

    async def confirm(
        self,
        chat: ChatRef,
        prompt: str,
        action_id: str,
        requester: UserRef,
        *,
        reply_to: MessageRef | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        """Show Confirm/Cancel buttons carrying only ``action_id``. Only called when
        ``capabilities.buttons`` is true; otherwise the core sends a reply-code prompt instead."""

    async def download(self, attachment: Attachment, *, chat: ChatRef, max_bytes: int) -> bytes:
        """Fetch a shared file, raising ``PlatformError`` past ``max_bytes``."""

    async def is_member(self, chat: ChatRef, user: UserRef) -> bool:
        """Is ``user`` currently a member of ``chat``? Used for DM access (§3)."""

    async def leave(self, chat: ChatRef) -> None: ...

    async def pin(self, message: MessageRef) -> None:
        """Only called when ``capabilities.pin`` is true."""
