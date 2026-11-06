"""Chat-scoped memory persistence; Postgres is authoritative and the dict backend supports tests."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Protocol

from chatcore.events import ChatRef, UserRef
from chatcore.store.base import Database


class MemoryRepo(Protocol):
    async def list(self, chat: ChatRef, *, limit: int = 50) -> list[dict[str, Any]]: ...
    async def audit_overwritten(self, chat: ChatRef) -> list[int]: ...
    async def get(self, chat: ChatRef, memory_id: int) -> dict[str, Any] | None: ...
    async def insert(
        self,
        chat: ChatRef,
        text: str,
        *,
        source: str,
        pinned: bool,
        created_by: UserRef,
        subject_user: UserRef | None = None,
    ) -> dict[str, Any]: ...
    async def update(
        self, chat: ChatRef, memory_id: int, text: str, *, pinned: bool | None = None
    ) -> dict[str, Any] | None: ...
    async def delete(self, chat: ChatRef, memory_id: int) -> dict[str, Any] | None: ...
    async def match(self, chat: ChatRef, query: str, *, limit: int) -> list[dict[str, Any]]: ...
    async def pinned(self, chat: ChatRef, *, limit: int) -> list[dict[str, Any]]: ...
    async def purge_user(self, chat: ChatRef, user: UserRef) -> list[int]: ...
    async def purge_chat(self, chat: ChatRef) -> list[int]: ...


class MemoryMemoryRepo:
    """Keep isolated rows for local use without a database."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    async def list(self, chat: ChatRef, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = [row for row in self.rows.values() if row["chat_key"] == chat.key]
        return [dict(row) for row in sorted(rows, key=lambda row: row["id"], reverse=True)[:limit]]

    async def audit_overwritten(self, chat: ChatRef) -> list[int]:
        return sorted(
            int(row["id"])
            for row in self.rows.values()
            if row["chat_key"] == chat.key
            and row["source"] == "auto"
            and row["updated_at"] > row["created_at"]
        )

    async def get(self, chat: ChatRef, memory_id: int) -> dict[str, Any] | None:
        row = self.rows.get(memory_id)
        return dict(row) if row and row["chat_key"] == chat.key else None

    async def insert(
        self,
        chat: ChatRef,
        text: str,
        *,
        source: str,
        pinned: bool,
        created_by: UserRef,
        subject_user: UserRef | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        row = {
            "id": self._next_id,
            "chat_key": chat.key,
            "subject_user_key": subject_user.key if subject_user else None,
            "text": text,
            "source": source,
            "pinned": pinned,
            "created_by": created_by.key,
            "created_at": now,
            "updated_at": now,
        }
        self.rows[self._next_id] = row
        self._next_id += 1
        return dict(row)

    async def update(
        self, chat: ChatRef, memory_id: int, text: str, *, pinned: bool | None = None
    ) -> dict[str, Any] | None:
        row = self.rows.get(memory_id)
        if not row or row["chat_key"] != chat.key:
            return None
        row["text"] = text
        if pinned is not None:
            row["pinned"] = pinned
        row["updated_at"] = datetime.now(UTC)
        return dict(row)

    async def delete(self, chat: ChatRef, memory_id: int) -> dict[str, Any] | None:
        row = await self.get(chat, memory_id)
        if row:
            self.rows.pop(memory_id)
        return row

    async def match(self, chat: ChatRef, query: str, *, limit: int) -> list[dict[str, Any]]:
        words = set(_words(query))
        ranked = []
        for row in self.rows.values():
            if row["chat_key"] != chat.key:
                continue
            count = len(words.intersection(_words(row["text"])))
            if count:
                ranked.append((count, row))
        ranked.sort(key=lambda pair: (-pair[0], -pair[1]["id"]))
        return [dict(row) for _, row in ranked[:limit]]

    async def pinned(self, chat: ChatRef, *, limit: int) -> list[dict[str, Any]]:
        rows = [r for r in self.rows.values() if r["chat_key"] == chat.key and r["pinned"]]
        return [dict(r) for r in sorted(rows, key=lambda r: r["id"], reverse=True)[:limit]]

    async def purge_user(self, chat: ChatRef, user: UserRef) -> list[int]:
        ids = [
            i
            for i, r in self.rows.items()
            if r["chat_key"] == chat.key
            and (
                r["subject_user_key"] == user.key
                or (r["source"] == "auto" and r["created_by"] == user.key)
            )
        ]
        for i in ids:
            self.rows.pop(i)
        return ids

    async def purge_chat(self, chat: ChatRef) -> list[int]:
        ids = [i for i, r in self.rows.items() if r["chat_key"] == chat.key]
        for i in ids:
            self.rows.pop(i)
        return ids


class PostgresMemoryRepo:
    """Persist facts with mandatory chat predicates on all row access."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def list(self, chat: ChatRef, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT * FROM memories WHERE chat_key = $1 ORDER BY id DESC LIMIT $2", chat.key, limit
        )
        return [dict(row) for row in rows]

    async def audit_overwritten(self, chat: ChatRef) -> list[int]:
        rows = await self.db.fetch(
            "SELECT id FROM memories WHERE chat_key=$1 AND source='auto' "
            "AND updated_at > created_at ORDER BY id ASC",
            chat.key,
        )
        return [int(row["id"]) for row in rows]

    async def get(self, chat: ChatRef, memory_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            "SELECT * FROM memories WHERE chat_key = $1 AND id = $2", chat.key, memory_id
        )
        return dict(row) if row else None

    async def insert(
        self,
        chat: ChatRef,
        text: str,
        *,
        source: str,
        pinned: bool,
        created_by: UserRef,
        subject_user: UserRef | None = None,
    ) -> dict[str, Any]:
        row = await self.db.fetchrow(
            """INSERT INTO memories (chat_key, subject_user_key, text, source, pinned, created_by)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING *""",
            chat.key,
            subject_user.key if subject_user else None,
            text,
            source,
            pinned,
            created_by.key,
        )
        return dict(row)

    async def update(
        self, chat: ChatRef, memory_id: int, text: str, *, pinned: bool | None = None
    ) -> dict[str, Any] | None:
        if pinned is None:
            row = await self.db.fetchrow(
                "UPDATE memories SET text=$3, updated_at=now() "
                "WHERE chat_key=$1 AND id=$2 RETURNING *",
                chat.key,
                memory_id,
                text,
            )
        else:
            row = await self.db.fetchrow(
                "UPDATE memories SET text=$3, pinned=$4, updated_at=now() "
                "WHERE chat_key=$1 AND id=$2 RETURNING *",
                chat.key,
                memory_id,
                text,
                pinned,
            )
        return dict(row) if row else None

    async def delete(self, chat: ChatRef, memory_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            "DELETE FROM memories WHERE chat_key=$1 AND id=$2 RETURNING *", chat.key, memory_id
        )
        return dict(row) if row else None

    async def match(self, chat: ChatRef, query: str, *, limit: int) -> list[dict[str, Any]]:
        words = list(dict.fromkeys(_query_words(query)))
        if not words:
            return []
        escaped_words = [
            word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for word in words
        ]
        rows = await self.db.fetch(
            """SELECT memories.*,
                (SELECT count(*) FROM unnest($2::text[]) AS word
                 WHERE memories.text ILIKE '%' || word || '%' ESCAPE chr(92)) AS match_count
            FROM memories
            WHERE chat_key=$1 AND EXISTS (
                SELECT 1 FROM unnest($2::text[]) AS word
                WHERE memories.text ILIKE '%' || word || '%' ESCAPE chr(92)
            )
            ORDER BY match_count DESC, created_at DESC LIMIT $3""",
            chat.key,
            escaped_words,
            limit,
        )
        return [dict(row) for row in rows]

    async def pinned(self, chat: ChatRef, *, limit: int) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT * FROM memories WHERE chat_key=$1 AND pinned=true ORDER BY id DESC LIMIT $2",
            chat.key,
            limit,
        )
        return [dict(row) for row in rows]

    async def purge_user(self, chat: ChatRef, user: UserRef) -> list[int]:
        rows = await self.db.fetch(
            "DELETE FROM memories WHERE chat_key=$1 AND (subject_user_key=$2 "
            "OR (source='auto' AND created_by=$2)) RETURNING id",
            chat.key,
            user.key,
        )
        return [int(row["id"]) for row in rows]

    async def purge_chat(self, chat: ChatRef) -> list[int]:
        rows = await self.db.fetch("DELETE FROM memories WHERE chat_key=$1 RETURNING id", chat.key)
        return [int(row["id"]) for row in rows]


def _words(text: str) -> list[str]:
    return re.findall(r"[\w@-]+", text.casefold())


def _query_words(text: str) -> list[str]:
    return re.findall(r"[\w@%\\-]+", text.casefold())
