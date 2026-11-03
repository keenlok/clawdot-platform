"""Postgres backend (asyncpg): the ``Database`` wrapper and the core repository."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import asyncpg

from chatcore.attribution import mention_data, mentions_for_source, mentions_from_data
from chatcore.events import Attachment, AttachmentKind, ChatRef, IncomingMessage, User, UserRef
from chatcore.pending import PendingAction
from chatcore.store.base import ChatSettings, Row, StoredMessage


class PgDatabase:
    def __init__(self, conn: asyncpg.Pool | asyncpg.Connection) -> None:
        self._conn = conn

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 5) -> PgDatabase:
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        return cls(pool)

    async def execute(self, sql: str, *args: Any) -> str:
        return await self._conn.execute(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[Row]:
        return list(await self._conn.fetch(sql, *args))

    async def fetchrow(self, sql: str, *args: Any) -> Row | None:
        return await self._conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetchval(sql, *args)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PgDatabase]:
        if isinstance(self._conn, asyncpg.Pool):
            async with self._conn.acquire() as conn, conn.transaction():
                yield PgDatabase(conn)
        else:
            async with self._conn.transaction():
                yield self

    async def close(self) -> None:
        if isinstance(self._conn, asyncpg.Pool):
            await self._conn.close()


def _pending_from_row(row: Row) -> PendingAction:
    return PendingAction(
        id=row["id"],
        chat=ChatRef.parse(row["chat_key"]),
        requester=UserRef.parse(row["requester_key"]),
        plugin=row["plugin"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        summary=row["summary"],
        reply_code=row["reply_code"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        used_at=row["used_at"],
        thread_id=row["thread_id"],
    )


def _message_from_row(row: Row) -> StoredMessage:
    mention_value = row.get("mentions", "[]")
    if isinstance(mention_value, str):
        try:
            mention_value = json.loads(mention_value)
        except (TypeError, ValueError):
            mention_value = []
    return StoredMessage(
        chat=ChatRef.parse(row["chat_key"]),
        message_id=row["message_id"],
        user=UserRef.parse(row["user_key"]),
        display_name=row["display_name"],
        text=row["text"],
        sent_at=row["sent_at"],
        thread_id=row["thread_id"],
        is_bot=row["is_bot"],
        attachments=tuple(
            Attachment(
                id=a["id"],
                kind=AttachmentKind(a["kind"]),
                filename=a.get("filename"),
                mime_type=a.get("mime_type"),
                size_bytes=a.get("size_bytes"),
            )
            for a in json.loads(row["attachments"])
        ),
        reply_to_id=row["reply_to_id"],
        username=row.get("username"),
        mentions=mentions_for_source(mentions_from_data(mention_value), row["text"]),
    )


class PgCoreRepo:
    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    async def log_message(self, message: IncomingMessage) -> None:
        attachments = [
            {
                "id": a.id,
                "kind": a.kind.value,
                "filename": a.filename,
                "mime_type": a.mime_type,
                "size_bytes": a.size_bytes,
            }
            for a in message.attachments
        ]
        mentions = mention_data(mentions_for_source(message.mentions, message.text))
        await self.db.execute(
            """INSERT INTO messages (chat_key, message_id, thread_id, user_key, display_name, text,
                                     attachments, reply_to_id, is_bot, sent_at, username, mentions)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12::jsonb)
               ON CONFLICT (chat_key, message_id) DO NOTHING""",
            message.chat.key,
            message.message_id,
            message.thread_id,
            message.user.ref.key,
            message.user.display_name,
            message.text,
            json.dumps(attachments),
            message.reply_to.message_id
            if message.reply_to and message.reply_to.chat == message.chat
            else None,
            message.user.is_bot,
            message.sent_at,
            message.user.username,
            json.dumps(mentions),
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
        await self.db.execute(
            """INSERT INTO messages (chat_key, message_id, thread_id, user_key, display_name, text,
                                     reply_to_id, is_bot, sent_at)
               VALUES ($1, $2, $3, $4, 'bot', $5, $6, true, now())
               ON CONFLICT (chat_key, message_id) DO NOTHING""",
            chat.key,
            message_id,
            thread_id,
            f"{chat.platform}:bot",
            text,
            reply_to_id,
        )

    async def recent_messages(
        self, chat: ChatRef, *, limit: int, thread_id: str | None = None
    ) -> list[StoredMessage]:
        rows = await self.db.fetch(
            """SELECT * FROM (
                   SELECT * FROM messages
                   WHERE chat_key = $1 AND ($2::text IS NULL OR thread_id = $2)
                   ORDER BY sent_at DESC, id DESC LIMIT $3
               ) recent ORDER BY sent_at, id""",
            chat.key,
            thread_id,
            limit,
        )
        return [_message_from_row(r) for r in rows]

    async def get_message(self, chat: ChatRef, message_id: str) -> StoredMessage | None:
        row = await self.db.fetchrow(
            "SELECT * FROM messages WHERE chat_key = $1 AND message_id = $2", chat.key, message_id
        )
        return _message_from_row(row) if row else None

    async def upsert_member(self, chat: ChatRef, user: User) -> None:
        await self.db.execute(
            """INSERT INTO members (chat_key, user_key, username, display_name, is_bot)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (chat_key, user_key) DO UPDATE
               SET username = EXCLUDED.username, display_name = EXCLUDED.display_name,
                   last_seen = now(), left_at = NULL""",
            chat.key,
            user.ref.key,
            user.username,
            user.display_name,
            user.is_bot,
        )

    async def mark_member_left(self, chat: ChatRef, user: UserRef) -> None:
        await self.db.execute(
            "UPDATE members SET left_at = now() WHERE chat_key = $1 AND user_key = $2",
            chat.key,
            user.key,
        )

    async def find_members(self, chat: ChatRef) -> list[User]:
        rows = await self.db.fetch(
            """SELECT user_key, username, display_name, is_bot FROM members
               WHERE chat_key = $1 AND left_at IS NULL ORDER BY display_name""",
            chat.key,
        )
        return [
            User(UserRef.parse(r["user_key"]), r["display_name"], r["username"], r["is_bot"])
            for r in rows
        ]

    async def get_chat_settings(self, chat: ChatRef) -> ChatSettings:
        row = await self.db.fetchrow(
            "SELECT persona, chatmode FROM chat_settings WHERE chat_key = $1", chat.key
        )
        if row is None:
            return ChatSettings(chat=chat)
        return ChatSettings(chat=chat, persona=row["persona"], chatmode=row["chatmode"])

    async def save_chat_settings(self, settings: ChatSettings, *, updated_by: UserRef) -> None:
        await self.db.execute(
            """INSERT INTO chat_settings (chat_key, persona, chatmode, updated_by)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (chat_key) DO UPDATE
               SET persona = EXCLUDED.persona, chatmode = EXCLUDED.chatmode,
                   updated_by = EXCLUDED.updated_by, updated_at = now()""",
            settings.chat.key,
            settings.persona,
            settings.chatmode,
            updated_by.key,
        )

    async def save_pending(self, action: PendingAction) -> None:
        await self.db.execute(
            """INSERT INTO pending_actions (id, chat_key, thread_id, requester_key, plugin, kind,
                                            payload, summary, reply_code, created_at, expires_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11)""",
            action.id,
            action.chat.key,
            action.thread_id,
            action.requester.key,
            action.plugin,
            action.kind,
            json.dumps(action.payload),
            action.summary,
            action.reply_code,
            action.created_at,
            action.expires_at,
        )

    async def get_pending(self, chat: ChatRef, action_id: str) -> PendingAction | None:
        row = await self.db.fetchrow(
            "SELECT * FROM pending_actions WHERE id = $1 AND chat_key = $2", action_id, chat.key
        )
        return _pending_from_row(row) if row else None

    async def find_pending_by_code(
        self, chat: ChatRef, requester: UserRef, code: str
    ) -> PendingAction | None:
        row = await self.db.fetchrow(
            """SELECT * FROM pending_actions
               WHERE chat_key = $1 AND requester_key = $2 AND reply_code = $3
                 AND used_at IS NULL AND expires_at > $4
               ORDER BY created_at DESC LIMIT 1""",
            chat.key,
            requester.key,
            code,
            datetime.now(UTC),
        )
        return _pending_from_row(row) if row else None

    async def claim_pending(self, chat: ChatRef, action_id: str) -> PendingAction | None:
        # Expiry uses the app's clock (it set expires_at), never the DB server's.
        now = datetime.now(UTC)
        row = await self.db.fetchrow(
            """UPDATE pending_actions SET used_at = $3
               WHERE id = $1 AND chat_key = $2 AND used_at IS NULL AND expires_at > $3
               RETURNING *""",
            action_id,
            chat.key,
            now,
        )
        return _pending_from_row(row) if row else None

    async def purge_user(self, chat: ChatRef, user: UserRef) -> int:
        async with self.db.transaction() as tx:
            messages = await tx.execute(
                "DELETE FROM messages WHERE chat_key=$1 AND user_key=$2", chat.key, user.key
            )
            await tx.execute("UPDATE messages SET mentions='[]'::jsonb WHERE chat_key=$1", chat.key)
            pending = await tx.execute(
                "DELETE FROM pending_actions WHERE chat_key=$1 AND requester_key=$2",
                chat.key,
                user.key,
            )
        return _deleted(messages) + _deleted(pending)

    async def purge_chat(self, chat: ChatRef) -> int:
        async with self.db.transaction() as tx:
            count = _deleted(await tx.execute("DELETE FROM messages WHERE chat_key=$1", chat.key))
            count += _deleted(
                await tx.execute("DELETE FROM pending_actions WHERE chat_key=$1", chat.key)
            )
            count += _deleted(
                await tx.execute("DELETE FROM chat_settings WHERE chat_key=$1", chat.key)
            )
            count += _deleted(await tx.execute("DELETE FROM members WHERE chat_key=$1", chat.key))
        return count

    async def purge_older_than(self, cutoff: datetime, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        async with self.db.transaction() as tx:
            messages = await tx.execute("DELETE FROM messages WHERE sent_at < $1", cutoff)
            pending = await tx.execute(
                "DELETE FROM pending_actions WHERE used_at IS NOT NULL OR expires_at < $1", now
            )
        return _deleted(messages) + _deleted(pending)


def _deleted(status: str) -> int:
    """Read asyncpg's affected-row count without exposing database details."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0
