"""Test helpers shared by every package: a recording fake platform, a scripted LLM provider, and
message builders. Import from tests only."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field

from chatcore.events import (
    Attachment,
    ButtonPress,
    ChatRef,
    Event,
    IncomingMessage,
    MessageRef,
    User,
    UserRef,
)
from chatcore.llm.types import Completion, Message, ToolChoice, ToolSpec
from chatcore.platform import Capabilities, EventHandler, PlatformError
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo

PLATFORM = "fake"
GROUP = ChatRef(PLATFORM, "group")


def user(name: str = "alice", *, is_bot: bool = False) -> User:
    return User(UserRef(PLATFORM, name), display_name=name.title(), username=name, is_bot=is_bot)


_ids = itertools.count(1)


def message(
    text: str,
    *,
    sender: User | None = None,
    chat: ChatRef = GROUP,
    dm: bool = False,
    mention: bool = False,
    reply_to_bot: bool = False,
    attachments: Sequence[Attachment] = (),
    thread_id: str | None = None,
) -> IncomingMessage:
    sender = sender or user()
    if dm:
        chat = ChatRef(PLATFORM, sender.ref.id)
    return IncomingMessage(
        chat=chat,
        message_id=str(next(_ids)),
        user=sender,
        text=text,
        is_dm=dm,
        thread_id=thread_id,
        attachments=tuple(attachments),
        reply_to=MessageRef(chat, "bot-msg") if reply_to_bot else None,
        reply_to_bot=reply_to_bot,
        mentions_bot=mention,
    )


def press(
    action_id: str, *, sender: User | None = None, chat: ChatRef = GROUP, confirm: bool = True
) -> ButtonPress:
    return ButtonPress(
        chat=chat,
        user=sender or user(),
        action_id=action_id,
        choice="confirm" if confirm else "cancel",
    )


@dataclass
class Sent:
    chat: ChatRef
    text: str
    ref: MessageRef
    reply_to: MessageRef | None = None
    action_id: str | None = None


@dataclass
class SentFile:
    chat: ChatRef
    filename: str
    data: bytes
    caption: str | None
    thread_id: str | None
    ref: MessageRef


@dataclass
class SentVoice:
    chat: ChatRef
    filename: str
    data: bytes
    caption: str | None
    thread_id: str | None
    ref: MessageRef


@dataclass
class FakePlatform:
    """Records everything the core asks the chat app to do."""

    name: str = PLATFORM
    capabilities: Capabilities = field(
        default_factory=lambda: Capabilities(
            buttons=True,
            edit=True,
            threads=True,
            files=True,
            dm=True,
            max_message_len=4096,
            pin=True,
        )
    )
    members: dict[str, set[str]] = field(default_factory=dict)
    """chat key → user keys; ``is_member`` answers from this."""
    files: dict[str, bytes] = field(default_factory=dict)
    sent: list[Sent] = field(default_factory=list)
    sent_files: list[SentFile] = field(default_factory=list)
    sent_voices: list[SentVoice] = field(default_factory=list)
    edits: list[tuple[MessageRef, str]] = field(default_factory=list)
    left: list[ChatRef] = field(default_factory=list)
    typing_calls: int = 0
    pinned: list[MessageRef] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    @property
    def texts(self) -> list[str]:
        return [s.text for s in self.sent]

    async def start(self, handler: EventHandler) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(
        self,
        chat: ChatRef,
        text: str,
        *,
        reply_to: MessageRef | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        ref = MessageRef(chat, f"b{next(self._ids)}", thread_id)
        self.sent.append(Sent(chat, text, ref, reply_to))
        return ref

    async def send_file(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        ref = MessageRef(chat, f"b{next(self._ids)}", thread_id)
        self.sent_files.append(SentFile(chat, filename, data, caption, thread_id, ref))
        return ref

    async def send_voice(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        ref = MessageRef(chat, f"b{next(self._ids)}", thread_id)
        self.sent_voices.append(SentVoice(chat, filename, data, caption, thread_id, ref))
        return ref

    async def edit(self, message: MessageRef, text: str) -> None:
        self.edits.append((message, text))

    async def typing(self, chat: ChatRef, thread_id: str | None = None) -> None:
        self.typing_calls += 1

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
        ref = MessageRef(chat, f"b{next(self._ids)}", thread_id)
        self.sent.append(Sent(chat, prompt, ref, reply_to, action_id))
        return ref

    async def download(self, attachment: Attachment, *, chat: ChatRef, max_bytes: int) -> bytes:
        data = self.files.get(attachment.id)
        if data is None:
            raise PlatformError("no such file")
        if len(data) > max_bytes:
            raise PlatformError("file too large")
        return data

    async def is_member(self, chat: ChatRef, user: UserRef) -> bool:
        return user.key in self.members.get(chat.key, set())

    async def leave(self, chat: ChatRef) -> None:
        self.left.append(chat)

    async def pin(self, message: MessageRef) -> None:
        self.pinned.append(message)


def memory_stores() -> Stores:
    return Stores(repo=MemoryCoreRepo(), cache=MemoryCache(prefix="test:"))


@dataclass
class ScriptedProvider:
    """An LLM provider that replays queued completions and records every request."""

    responses: list[Completion] = field(default_factory=list)
    name: str = "scripted"
    calls: list[dict] = field(default_factory=list)

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
        self.calls.append(
            {
                "messages": list(messages),
                "system": system,
                "tools": list(tools),
                "tool_choice": tool_choice,
                "max_tokens": max_tokens,
                "session_key": session_key,
            }
        )
        if not self.responses:
            raise AssertionError("ScriptedProvider ran out of responses")
        return self.responses.pop(0)


async def feed(handler: EventHandler, *events: Event) -> None:
    for event in events:
        await handler(event)
