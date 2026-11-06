"""In-memory backends: tests, the console adapter, and small bots with no services."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

from chatcore.attribution import mentions_for_source
from chatcore.events import ChatRef, IncomingMessage, User, UserRef
from chatcore.pending import PendingAction
from chatcore.store.base import ChatSettings, StoredMessage


class MemoryCache:
    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix
        self._data: dict[str, tuple[bytes, float]] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _k(self, key: str) -> str:
        return self._prefix + key

    def _live(self, key: str) -> tuple[bytes, float] | None:
        item = self._data.get(key)
        if item and item[1] <= time.monotonic():
            del self._data[key]
            return None
        return item

    async def get(self, key: str) -> bytes | None:
        item = self._live(self._k(key))
        return item[0] if item else None

    async def set(self, key: str, value: bytes, *, ttl_s: int) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self._data[self._k(key)] = (value, time.monotonic() + ttl_s)

    async def incr(self, key: str, *, ttl_s: int) -> int:
        k = self._k(key)
        item = self._live(k)
        if item is None:
            self._data[k] = (b"1", time.monotonic() + ttl_s)
            return 1
        value = int(item[0]) + 1
        self._data[k] = (str(value).encode(), item[1])
        return value

    async def delete(self, key: str) -> None:
        self._data.pop(self._k(key), None)

    @asynccontextmanager
    async def lock(self, key: str, *, ttl_s: int) -> AsyncIterator[None]:
        lock = self._locks[self._k(key)]
        await asyncio.wait_for(lock.acquire(), timeout=ttl_s)
        try:
            yield
        finally:
            lock.release()


class MemoryCoreRepo:
    def __init__(self) -> None:
        self._messages: dict[str, list[StoredMessage]] = defaultdict(list)
        self._members: dict[str, dict[str, tuple[User, bool]]] = defaultdict(dict)
        self._settings: dict[str, ChatSettings] = {}
        self._pending: dict[str, PendingAction] = {}
        self._erasures: dict[str, dict] = {}

    async def log_message(self, message: IncomingMessage) -> None:
        self._messages[message.chat.key].append(
            StoredMessage(
                chat=message.chat,
                message_id=message.message_id,
                user=message.user.ref,
                display_name=message.user.display_name,
                text=message.text,
                sent_at=message.sent_at,
                thread_id=message.thread_id,
                is_bot=message.user.is_bot,
                attachments=message.attachments,
                reply_to_id=(
                    message.reply_to.message_id
                    if message.reply_to and message.reply_to.chat == message.chat
                    else None
                ),
                username=message.user.username,
                mentions=mentions_for_source(message.mentions, message.text),
            )
        )

    async def log_bot_message(
        self,
        chat: ChatRef,
        message_id: str,
        text: str,
        *,
        thread_id: str | None,
        reply_to_id: str | None = None,
    ) -> None:
        self._messages[chat.key].append(
            StoredMessage(
                chat=chat,
                message_id=message_id,
                user=UserRef(chat.platform, "bot"),
                display_name="bot",
                text=text,
                sent_at=datetime.now(UTC),
                thread_id=thread_id,
                is_bot=True,
                reply_to_id=reply_to_id,
            )
        )

    async def recent_messages(
        self, chat: ChatRef, *, limit: int, thread_id: str | None = None
    ) -> list[StoredMessage]:
        rows = [
            m for m in self._messages[chat.key] if thread_id is None or m.thread_id == thread_id
        ]
        return rows[-limit:]

    async def get_message(self, chat: ChatRef, message_id: str) -> StoredMessage | None:
        return next((m for m in self._messages[chat.key] if m.message_id == message_id), None)

    async def create_erasure(self, operation_id, chat, scope, target_user_key):
        active = any(
            row["chat_key"] == chat.key and row["status"] == "active"
            for row in self._erasures.values()
        )
        row = {
            "operation_id": operation_id,
            "chat_key": chat.key,
            "scope": scope,
            "target_user_key": target_user_key,
            "status": "queued" if active else "active",
            "completed_steps": [],
            "attempts": 0,
            "next_attempt_at": datetime.now(UTC),
            "last_error_code": None,
            "created_at": datetime.now(UTC),
        }
        self._erasures[operation_id] = row
        return dict(row)

    async def list_erasures(self, chat: ChatRef | None = None):
        return [
            dict(row)
            for row in self._erasures.values()
            if chat is None or row["chat_key"] == chat.key
        ]

    async def update_erasure(self, operation_id: str, **fields: object) -> None:
        self._erasures[operation_id].update(fields)

    async def delete_erasure(self, operation_id: str) -> None:
        self._erasures.pop(operation_id, None)

    async def promote_erasure(self, chat: ChatRef):
        if any(
            row["chat_key"] == chat.key and row["status"] == "active"
            for row in self._erasures.values()
        ):
            return None
        queued = sorted(
            (
                row
                for row in self._erasures.values()
                if row["chat_key"] == chat.key and row["status"] == "queued"
            ),
            key=lambda row: row["created_at"],
        )
        if not queued:
            return None
        queued[0]["status"] = "active"
        return dict(queued[0])

    async def human_source(self, chat: ChatRef, message_id: str) -> tuple[str, bool] | None:
        row = await self.get_message(chat, message_id)
        return (row.user.key, row.is_bot) if row else None

    async def upsert_member(self, chat: ChatRef, user: User) -> None:
        self._members[chat.key][user.ref.key] = (user, True)

    async def mark_member_left(self, chat: ChatRef, user: UserRef) -> None:
        entry = self._members[chat.key].get(user.key)
        if entry:
            self._members[chat.key][user.key] = (entry[0], False)

    async def find_members(self, chat: ChatRef) -> list[User]:
        return [u for u, active in self._members[chat.key].values() if active]

    async def get_chat_settings(self, chat: ChatRef) -> ChatSettings:
        return self._settings.get(chat.key, ChatSettings(chat=chat))

    async def save_chat_settings(self, settings: ChatSettings, *, updated_by: UserRef) -> None:
        self._settings[settings.chat.key] = settings

    async def save_pending(self, action: PendingAction) -> None:
        self._pending[action.id] = action

    async def get_pending(self, chat: ChatRef, action_id: str) -> PendingAction | None:
        action = self._pending.get(action_id)
        return action if action and action.chat == chat else None

    async def find_pending_by_code(
        self, chat: ChatRef, requester: UserRef, code: str
    ) -> PendingAction | None:
        now = datetime.now(UTC)
        for action in reversed(self._pending.values()):
            if (
                action.chat == chat
                and action.requester == requester
                and action.reply_code == code
                and action.used_at is None
                and not action.is_expired(now)
            ):
                return action
        return None

    async def claim_pending(self, chat: ChatRef, action_id: str) -> PendingAction | None:
        action = await self.get_pending(chat, action_id)
        now = datetime.now(UTC)
        if action is None or action.used_at is not None or action.is_expired(now):
            return None
        claimed = action.mark_used(now)
        self._pending[action_id] = claimed
        return claimed

    async def purge_user(self, chat: ChatRef, user: UserRef) -> int:
        messages = self._messages.get(chat.key, [])
        kept = [replace(row, mentions=()) for row in messages if row.user != user]
        removed = len(messages) - len(kept)
        self._messages[chat.key] = kept
        pending_ids = [
            action_id
            for action_id, action in self._pending.items()
            if action.chat == chat and action.requester == user
        ]
        for action_id in pending_ids:
            del self._pending[action_id]
        return removed + len(pending_ids)

    async def purge_chat(self, chat: ChatRef) -> int:
        removed = len(self._messages.pop(chat.key, [])) + len(self._members.pop(chat.key, {}))
        if self._settings.pop(chat.key, None) is not None:
            removed += 1
        pending_ids = [
            action_id for action_id, action in self._pending.items() if action.chat == chat
        ]
        for action_id in pending_ids:
            del self._pending[action_id]
        return removed + len(pending_ids)

    async def purge_older_than(self, cutoff: datetime, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        removed = 0
        for chat_key, messages in list(self._messages.items()):
            kept = [row for row in messages if row.sent_at >= cutoff]
            removed += len(messages) - len(kept)
            self._messages[chat_key] = kept
        pending_ids = [
            action_id
            for action_id, action in self._pending.items()
            if action.used_at is not None or action.expires_at < now
        ]
        for action_id in pending_ids:
            del self._pending[action_id]
        return removed + len(pending_ids)
