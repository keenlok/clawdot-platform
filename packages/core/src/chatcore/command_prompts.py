"""Replies as command input (``plans/command-replies.md``).

A command that needs more input returns ``CommandReply(text, awaits_reply=True)``. The bot sends
the text and remembers the sent message for ``PROMPT_TTL_S``; the next reply to it from anyone in
the chat becomes the command's argument text, as if they had typed ``/name <reply>``. The command
runs as the person who replied, with their permissions. The prompt record holds only the command
name, never message text.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from chatcore.events import ChatRef, MessageRef
from chatcore.store.base import Cache

PROMPT_TTL_S = 600
CANCEL_WORDS = frozenset({"cancel", "取消"})
CANCELLED_TEXT = "Cancelled."


@dataclass(frozen=True, slots=True)
class CommandReply:
    """What a command handler may return instead of a plain string."""

    text: str
    awaits_reply: bool = False
    """True: a reply to this message is the input for the same command."""
    placeholder: str = ""
    """Hint shown in the reply box where the platform supports reply prompts."""


def reply_text(result: str | CommandReply | None) -> str | None:
    """The text to send for a handler result, for callers that don't open prompts."""
    if isinstance(result, CommandReply):
        return result.text
    return result


@runtime_checkable
class ReplyPromptPlatform(Protocol):
    """Optional: send a message that opens the reply box (Telegram ``ForceReply``)."""

    async def send_reply_prompt(
        self,
        chat: ChatRef,
        text: str,
        *,
        reply_to: MessageRef | None = None,
        thread_id: str | None = None,
        placeholder: str = "",
    ) -> MessageRef: ...


class CommandPromptStore:
    """Open prompts, keyed by the bot message people reply to. Each is used once."""

    def __init__(self, cache: Cache, *, ttl_s: int = PROMPT_TTL_S) -> None:
        self.cache = cache
        self.ttl_s = ttl_s

    @staticmethod
    def _key(chat: ChatRef, message_id: str) -> str:
        return f"cmd-prompt:{chat.key}:{message_id}"

    async def open(self, chat: ChatRef, message_ids: Iterable[str], command: str) -> None:
        """Open one prompt for ``command`` on every part of a (possibly split) bot message."""
        ids = list(dict.fromkeys(message_ids))
        value = json.dumps({"command": command, "ids": ids}).encode()
        for message_id in ids:
            await self.cache.set(self._key(chat, message_id), value, ttl_s=self.ttl_s)

    async def take(self, chat: ChatRef, message_id: str) -> str | None:
        """Claim the prompt behind ``message_id``: the first reply wins, later ones get None."""
        key = self._key(chat, message_id)
        async with self.cache.lock(f"cmd-prompt-lock:{chat.key}", ttl_s=10):
            raw = await self.cache.get(key)
            if raw is None:
                return None
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                data = None
            ids = data.get("ids") if isinstance(data, dict) else None
            for sibling in ids if isinstance(ids, list) else []:
                if isinstance(sibling, str):
                    await self.cache.delete(self._key(chat, sibling))
            await self.cache.delete(key)
        command = data.get("command") if isinstance(data, dict) else None
        return command if isinstance(command, str) and command else None
