"""Chat-scoped memory persistence; Postgres is authoritative and the dict backend supports tests."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Protocol

from chatcore.events import ChatRef, UserRef
from chatcore.store.base import Database

from .service import jaccard, normalize_fact


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
    async def commit_fact(
        self,
        chat: ChatRef,
        text: str,
        *,
        source: str,
        pinned: bool,
        created_by: UserRef,
        subject_user: UserRef | None,
        candidate_ids: set[int],
        dedupe_threshold: float,
        replaces: int | None = None,
        expected_updated_at: Any = None,
        preserve_subject: bool = False,
    ) -> dict[str, Any]: ...
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

    async def commit_fact(
        self,
        chat: ChatRef,
        text: str,
        *,
        source: str,
        pinned: bool,
        created_by: UserRef,
        subject_user: UserRef | None,
        candidate_ids: set[int],
        dedupe_threshold: float,
        replaces: int | None = None,
        expected_updated_at: Any = None,
        preserve_subject: bool = False,
    ) -> dict[str, Any]:
        if replaces is not None:
            old = await self.get(chat, replaces)
            if old is None or old["updated_at"] != expected_updated_at:
                return {"status": "stale", "old": old}
            new_subject = (
                UserRef.parse(old["subject_user_key"])
                if preserve_subject and old.get("subject_user_key")
                else subject_user
            )
            self.rows.pop(replaces, None)
            row = await self.insert(
                chat,
                text,
                source=source,
                pinned=bool(old["pinned"] or pinned),
                created_by=created_by,
                subject_user=new_subject,
            )
            return {"status": "replaced", "row": row, "old": old}
        same = [
            dict(row)
            for row in self.rows.values()
            if row["chat_key"] == chat.key
            and row.get("subject_user_key") == (subject_user.key if subject_user else None)
        ]
        same.sort(key=lambda row: row["id"])
        exact_rows = [row for row in same if normalize_fact(row["text"]) == normalize_fact(text)]
        exact = next((row for row in exact_rows if row["created_by"] == created_by.key), None)
        near = next(
            (
                row
                for row in same
                if row["id"] in candidate_ids or jaccard(row["text"], text) >= 0.8
            ),
            None,
        )
        if exact:
            return {"status": "exact", "row": exact}
        if exact_rows and (source == "explicit" or subject_user):
            return {"status": "exact", "row": exact_rows[0]}
        if exact_rows and source == "auto":
            row = await self.insert(
                chat,
                text,
                source=source,
                pinned=pinned,
                created_by=created_by,
                subject_user=subject_user,
            )
            return {"status": "saved", "row": row}
        elif source == "explicit" and near:
            row = await self.insert(
                chat,
                text,
                source=source,
                pinned=pinned,
                created_by=created_by,
                subject_user=subject_user,
            )
            return {"status": "similar", "row": row, "similar": near}
        elif source == "auto" and near:
            return {"status": "near", "row": near}
        row = await self.insert(
            chat,
            text,
            source=source,
            pinned=pinned,
            created_by=created_by,
            subject_user=subject_user,
        )
        return {"status": "saved", "row": row}

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

    async def commit_fact(
        self,
        chat: ChatRef,
        text: str,
        *,
        source: str,
        pinned: bool,
        created_by: UserRef,
        subject_user: UserRef | None,
        candidate_ids: set[int],
        dedupe_threshold: float,
        replaces: int | None = None,
        expected_updated_at: Any = None,
        preserve_subject: bool = False,
    ) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            await tx.execute(
                "INSERT INTO memory_control (chat_key) VALUES ($1) ON CONFLICT DO NOTHING",
                chat.key,
            )
            await tx.fetchrow(
                "SELECT chat_key FROM memory_control WHERE chat_key=$1 FOR UPDATE", chat.key
            )
            if replaces is not None:
                old_row = await tx.fetchrow(
                    "SELECT * FROM memories WHERE chat_key=$1 AND id=$2", chat.key, replaces
                )
                old = dict(old_row) if old_row else None
                if old is None or old["updated_at"] != expected_updated_at:
                    return {"status": "stale", "old": old}
                new_subject = (
                    old.get("subject_user_key")
                    if preserve_subject
                    else (subject_user.key if subject_user else None)
                )
                await tx.execute(
                    "DELETE FROM memories WHERE chat_key=$1 AND id=$2", chat.key, replaces
                )
                row = await self._insert_tx(
                    tx,
                    chat,
                    text,
                    source=source,
                    pinned=bool(old["pinned"] or pinned),
                    created_by=created_by,
                    subject_key=new_subject,
                )
                return {"status": "replaced", "row": row, "old": old}

            same: list[dict[str, Any]] = []
            cursor = 0
            subject_key = subject_user.key if subject_user else None
            while True:
                rows = await tx.fetch(
                    "SELECT * FROM memories WHERE chat_key=$1 "
                    "AND subject_user_key IS NOT DISTINCT FROM $2 "
                    "AND id>$3 ORDER BY id LIMIT 500",
                    chat.key,
                    subject_key,
                    cursor,
                )
                if not rows:
                    break
                batch = [dict(row) for row in rows]
                same.extend(batch)
                cursor = int(batch[-1]["id"])
            # Hydrate vector candidates under the transaction and retain only same-identity rows.
            for memory_id in candidate_ids:
                if any(int(row["id"]) == memory_id for row in same):
                    continue
                row = await tx.fetchrow(
                    "SELECT * FROM memories WHERE chat_key=$1 AND id=$2 "
                    "AND subject_user_key IS NOT DISTINCT FROM $3",
                    chat.key,
                    memory_id,
                    subject_key,
                )
                if row:
                    same.append(dict(row))
            exact_rows = [
                row for row in same if normalize_fact(row["text"]) == normalize_fact(text)
            ]
            exact = next((row for row in exact_rows if row["created_by"] == created_by.key), None)
            near = next(
                (
                    row
                    for row in same
                    if int(row["id"]) in candidate_ids or jaccard(row["text"], text) >= 0.8
                ),
                None,
            )
            if exact:
                return {"status": "exact", "row": exact}
            if exact_rows and (source == "explicit" or subject_user):
                return {"status": "exact", "row": exact_rows[0]}
            if exact_rows and source == "auto":
                row = await self._insert_tx(
                    tx,
                    chat,
                    text,
                    source=source,
                    pinned=pinned,
                    created_by=created_by,
                    subject_key=subject_key,
                )
                return {"status": "saved", "row": row}
            elif source == "explicit" and near:
                row = await self._insert_tx(
                    tx,
                    chat,
                    text,
                    source=source,
                    pinned=pinned,
                    created_by=created_by,
                    subject_key=subject_key,
                )
                return {"status": "similar", "row": row, "similar": near}
            elif source == "auto" and near:
                return {"status": "near", "row": near}
            row = await self._insert_tx(
                tx,
                chat,
                text,
                source=source,
                pinned=pinned,
                created_by=created_by,
                subject_key=subject_key,
            )
            return {"status": "saved", "row": row}

    async def _insert_tx(self, tx, chat, text, *, source, pinned, created_by, subject_key):
        row = await tx.fetchrow(
            "INSERT INTO memories (chat_key, subject_user_key, text, source, pinned, created_by) "
            "VALUES ($1,$2,$3,$4,$5,$6) RETURNING *",
            chat.key,
            subject_key,
            text,
            source,
            pinned,
            created_by.key,
        )
        return dict(row)

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
