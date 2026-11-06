"""Reply-chain conversation selection for group chats."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from chatcore.events import MessageRef

if TYPE_CHECKING:
    from chatcore.conversation import Conversation, ConversationStore, ResetPoint
    from chatcore.events import IncomingMessage
    from chatcore.store.base import CoreRepo


@dataclass(frozen=True, slots=True)
class ThreadSettings:
    continue_window_s: int = 3600
    reopen_compact_after_s: int = 3600
    auto_compact_turns: int = 30
    auto_compact_chars: int = 16000
    auto_keep_turns: int = 12
    reopen_keep_turns: int = 4


REPLY_ANCESTRY_LIMIT = 32
NEW_CONVERSATION_TTL_S = 3600


def new_conversation_key(chat_key: str, thread_id: str | None, user_key: str) -> str:
    """Cache marker: this requester's next addressed message here starts a new conversation."""
    return f"conversation-new:{chat_key}:{thread_id or ''}:{user_key}"


@runtime_checkable
class MultipartSender(Protocol):
    def sent_message_ids(self, ref: MessageRef) -> tuple[str, ...]: ...


current_conversation: ContextVar[Conversation | None] = ContextVar(
    "current_conversation", default=None
)


@dataclass(frozen=True, slots=True)
class TurnOrigin:
    kind: Literal["direct", "bare_tag", "synthetic"]
    source_message_id: str | None = None


current_turn_origin: ContextVar[TurnOrigin | None] = ContextVar("current_turn_origin", default=None)


async def resolve_conversation(
    store: ConversationStore,
    repo: CoreRepo,
    message: IncomingMessage,
    *,
    now: datetime | None = None,
    continue_window_s: int = 3600,
    force_new: bool = False,
) -> tuple[Conversation, bool]:
    """Return the selected conversation and whether an old one was reopened."""

    now = now or message.sent_at or datetime.now(UTC)
    if force_new:
        conversation = await store.create_conversation(
            message.chat, message.thread_id, message.user.ref, at=now
        )
        return conversation, False

    conversation = None
    reopened = False
    if message.reply_to:
        point = await store.reset_point(message.chat, message.thread_id)
        conversation, boundary_requires_new = await _reply_conversation(store, repo, message, point)
        if conversation is not None:
            reopened = True
        elif boundary_requires_new:
            conversation = await store.create_conversation(
                message.chat, message.thread_id, message.user.ref, at=now
            )

    if conversation is None:
        since = now - timedelta(seconds=continue_window_s)
        conversation = await store.latest_conversation(
            message.chat, message.thread_id, message.user.ref, since=since
        )
        if conversation is None:
            conversation = await store.create_conversation(
                message.chat, message.thread_id, message.user.ref, at=now
            )
    return conversation, reopened


async def _reply_conversation(
    store: ConversationStore,
    repo: CoreRepo,
    message: IncomingMessage,
    point: ResetPoint | None,
) -> tuple[Conversation | None, bool]:
    """Follow a bounded same-chat reply chain to its first mapped conversation.

    Returns the conversation and whether a hard boundary requires a new one.
    Missing/deleted rows and exhausted or cyclic chains leave the existing
    sender-local fallback available.
    """
    ref = message.reply_to
    if ref is None:
        return None, False
    if ref.chat != message.chat:
        return None, True
    if ref.thread_id is not None and ref.thread_id != message.thread_id:
        return None, True

    seen: set[str] = set()
    reply_id = ref.message_id
    for _ in range(REPLY_ANCESTRY_LIMIT):
        if reply_id in seen:
            return None, False
        seen.add(reply_id)

        mapped = await store.conversation_for_message(message.chat, reply_id)
        if mapped is not None:
            candidate = mapped[0]
            if (
                candidate.chat == message.chat
                and candidate.thread_id == message.thread_id
                and candidate.reset_generation == (point.generation if point else 0)
            ):
                return candidate, False
            return None, True

        row = await repo.get_message(message.chat, reply_id)
        if row is None:
            return None, bool(message.reply_to_bot and len(seen) == 1)
        if (
            row.chat != message.chat
            or row.thread_id != message.thread_id
            or (point is not None and row.sent_at <= point.reset_at)
            or row.is_bot
            or (message.reply_to_bot and len(seen) == 1)
        ):
            return None, True
        if row.reply_to_id is None:
            return None, False
        reply_id = row.reply_to_id

    return None, False
