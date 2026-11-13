"""Normalized events that adapters produce. Nothing here knows about any chat platform.

Every chat and user is identified by a namespaced key, ``<platform>:<id>`` (e.g. ``tg:-100123``),
so several bots and platforms can share one database without collisions (§3d).

Convention: a DM's ``ChatRef`` has the same ``id`` as the other user's ``UserRef`` (as on Telegram).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal


def _split_key(key: str) -> tuple[str, str]:
    platform, sep, ident = key.partition(":")
    if not sep or not platform or not ident:
        raise ValueError(f"not a namespaced key: {key!r}")
    return platform, ident


@dataclass(frozen=True, slots=True)
class ChatRef:
    platform: str
    id: str

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.id}"

    @classmethod
    def parse(cls, key: str) -> ChatRef:
        return cls(*_split_key(key))

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True, slots=True)
class UserRef:
    platform: str
    id: str

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.id}"

    @classmethod
    def parse(cls, key: str) -> UserRef:
        return cls(*_split_key(key))

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True, slots=True)
class User:
    ref: UserRef
    display_name: str
    username: str | None = None
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class Mention:
    """A platform-verified mention span in the original human message text."""

    start: int
    end: int
    kind: Literal["mention", "text_mention"]
    text: str
    target: UserRef | None = None
    username: str | None = None
    display_name: str | None = None
    is_bot: bool = False


class AttachmentKind(StrEnum):
    PHOTO = "photo"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Attachment:
    """A file shared in chat. ``id`` is adapter-specific: pass it back to
    ``ChatPlatform.download``."""

    id: str
    kind: AttachmentKind
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class MessageRef:
    chat: ChatRef
    message_id: str
    thread_id: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    chat: ChatRef
    message_id: str
    user: User
    text: str
    is_dm: bool
    thread_id: str | None = None
    attachments: tuple[Attachment, ...] = ()
    reply_to: MessageRef | None = None
    # Set by the adapter: is this a reply to one of the bot's own messages?
    reply_to_bot: bool = False
    mentions_bot: bool = False
    sent_at: datetime = field(default_factory=_now)
    # Adapter-supplied group label. User-controlled metadata, never trusted instructions.
    chat_title: str | None = None
    mentions: tuple[Mention, ...] = ()

    @property
    def platform(self) -> str:
        return self.chat.platform

    @property
    def ref(self) -> MessageRef:
        return MessageRef(self.chat, self.message_id, self.thread_id)


@dataclass(frozen=True, slots=True)
class ButtonPress:
    """A Confirm/Cancel press on a pending action. Only ``action_id`` travels through the
    platform, never the payload."""

    chat: ChatRef
    user: User
    action_id: str
    choice: Literal["confirm", "cancel"]
    message: MessageRef | None = None


@dataclass(frozen=True, slots=True)
class MemberJoined:
    chat: ChatRef
    user: User


@dataclass(frozen=True, slots=True)
class MemberLeft:
    chat: ChatRef
    user: User


@dataclass(frozen=True, slots=True)
class BotAddedToChat:
    chat: ChatRef
    added_by: User | None = None
    # Adapter-authenticated bot/update identity; distinguishes replay from rejoin.
    event_key: str | None = None


@dataclass(frozen=True, slots=True)
class PollAnswer:
    """A vote change on a native poll this bot sent.

    ``poll_id`` is namespaced only by ``user.ref.platform``. Empty ``option_ids``
    means the vote was retracted.
    """

    poll_id: str
    user: User
    option_ids: tuple[int, ...]


Event = IncomingMessage | ButtonPress | MemberJoined | MemberLeft | BotAddedToChat | PollAnswer
