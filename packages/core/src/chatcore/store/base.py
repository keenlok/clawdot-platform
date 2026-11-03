"""Storage interfaces (§3d). Backends: Postgres (``pg``), in-memory (``memory``); Valkey and
Elasticsearch backends are added by tracks A and C.

Isolation rule (§6a): every per-chat read or write takes ``chat`` as a **required** argument.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from chatcore.events import Attachment, ChatRef, IncomingMessage, Mention, User, UserRef
from chatcore.pending import PendingAction

Row = Mapping[str, Any]


class Database(Protocol):
    """Relational access with asyncpg-style ``$1`` placeholders."""

    async def execute(self, sql: str, *args: Any) -> str: ...
    async def fetch(self, sql: str, *args: Any) -> list[Row]: ...
    async def fetchrow(self, sql: str, *args: Any) -> Row | None: ...
    async def fetchval(self, sql: str, *args: Any) -> Any: ...
    def transaction(self) -> AbstractAsyncContextManager[Database]: ...
    async def close(self) -> None: ...


class Cache(Protocol):
    """Ephemeral state. Every write takes a TTL: nothing may live forever (§3a)."""

    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, *, ttl_s: int) -> None: ...
    async def incr(self, key: str, *, ttl_s: int) -> int:
        """Increment and return; the TTL is set when the key is created."""

    async def delete(self, key: str) -> None: ...
    def lock(self, key: str, *, ttl_s: int) -> AbstractAsyncContextManager[None]:
        """Mutual exclusion across workers, released on exit or after ``ttl_s``."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    doc_id: str
    score: float
    source: Mapping[str, Any]
    highlights: Sequence[str] = ()


class SearchIndex(Protocol):
    supports_knn: bool

    async def index(
        self, index: str, doc_id: str, doc: Mapping[str, Any], *, chat: ChatRef
    ) -> None:
        """Store ``doc``; the backend adds ``chat_key`` itself."""

    async def search(
        self,
        index: str,
        *,
        chat: ChatRef,
        text: str | None = None,
        vector: Sequence[float] | None = None,
        filters: Mapping[str, Any] | None = None,
        size: int = 10,
    ) -> list[SearchHit]:
        """BM25 when ``text`` is given, kNN when ``vector`` is; always filtered to ``chat``."""

    async def delete(
        self, index: str, *, chat: ChatRef, query: Mapping[str, Any] | None = None
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class StoredMessage:
    chat: ChatRef
    message_id: str
    user: UserRef
    display_name: str
    text: str
    sent_at: datetime
    thread_id: str | None = None
    is_bot: bool = False
    attachments: tuple[Attachment, ...] = ()
    """Pass an attachment back to ``ChatPlatform.download`` to read it later."""
    reply_to_id: str | None = None
    username: str | None = None
    mentions: tuple[Mention, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatSettings:
    chat: ChatRef
    persona: str | None = None
    chatmode: bool = False


class CoreRepo(Protocol):
    """The core tables from ``chatcore/migrations``."""

    async def log_message(self, message: IncomingMessage) -> None: ...
    async def log_bot_message(
        self,
        chat: ChatRef,
        message_id: str,
        text: str,
        *,
        thread_id: str | None,
        reply_to_id: str | None = None,
    ) -> None: ...
    async def recent_messages(
        self, chat: ChatRef, *, limit: int, thread_id: str | None = None
    ) -> list[StoredMessage]: ...
    async def get_message(self, chat: ChatRef, message_id: str) -> StoredMessage | None: ...
    async def upsert_member(self, chat: ChatRef, user: User) -> None: ...
    async def mark_member_left(self, chat: ChatRef, user: UserRef) -> None: ...
    async def find_members(self, chat: ChatRef) -> list[User]: ...
    async def get_chat_settings(self, chat: ChatRef) -> ChatSettings: ...
    async def save_chat_settings(self, settings: ChatSettings, *, updated_by: UserRef) -> None: ...
    async def save_pending(self, action: PendingAction) -> None: ...
    async def get_pending(self, chat: ChatRef, action_id: str) -> PendingAction | None: ...
    async def find_pending_by_code(
        self, chat: ChatRef, requester: UserRef, code: str
    ) -> PendingAction | None: ...
    async def claim_pending(self, chat: ChatRef, action_id: str) -> PendingAction | None:
        """Atomically mark an unused, unexpired action as used and return it.

        Returns ``None`` if it can't be claimed (unknown, other chat, used or expired)."""

    async def purge_user(self, chat: ChatRef, user: UserRef) -> int:
        """Delete this user's messages and actions in chat; member rows stay as ledger names."""

    async def purge_chat(self, chat: ChatRef) -> int:
        """Delete messages, pending actions, settings and member rows for chat."""

    async def purge_older_than(self, cutoff: datetime, *, now: datetime | None = None) -> int:
        """Delete old messages and used or expired pending actions using the app clock."""


@dataclass(slots=True)
class Stores:
    repo: CoreRepo
    cache: Cache
    db: Database | None = None
    search: SearchIndex | None = None
    extra: dict[str, Any] = field(default_factory=dict)
