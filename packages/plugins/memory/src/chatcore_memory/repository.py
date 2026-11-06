"""Chat-scoped memory persistence; Postgres is authoritative and the dict backend supports tests."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from chatcore.events import ChatRef, UserRef
from chatcore.store.base import Database
from chatcore.store.evidence import human_source

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
        epoch: int | None = None,
        source_ids: list[tuple[str, str]] | None = None,
        provenance: str = "none",
        policy_version: int = 1,
        index_enabled: bool = True,
    ) -> dict[str, Any]: ...
    async def delete(
        self, chat: ChatRef, memory_id: int, *, index_enabled: bool = True
    ) -> dict[str, Any] | None: ...
    async def match(self, chat: ChatRef, query: str, *, limit: int) -> list[dict[str, Any]]: ...
    async def pinned(self, chat: ChatRef, *, limit: int) -> list[dict[str, Any]]: ...
    async def purge_user(
        self, chat: ChatRef, user: UserRef, *, index_enabled: bool = True
    ) -> list[int]: ...
    async def purge_chat(self, chat: ChatRef, *, index_enabled: bool = True) -> list[int]: ...
    async def capture_epoch(self, chat: ChatRef) -> int: ...
    async def erasure_active(self, chat: ChatRef) -> bool: ...
    async def human_source(self, chat: ChatRef, message_id: str) -> tuple[str, bool] | None: ...
    async def index_work(
        self, chat: ChatRef | None = None, *, due_only: bool = True
    ) -> list[dict[str, Any]]: ...
    async def finish_index_work(self, work_id: int) -> None: ...
    async def finish_index_work_for(
        self, chat: ChatRef, memory_id: int | None, op: str
    ) -> None: ...
    async def count_index_work(self, chat: ChatRef) -> int: ...
    async def fail_index_work(self, work_id: int) -> None: ...


class MemoryMemoryRepo:
    """Keep isolated rows for local use without a database."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self.sources: dict[int, list[dict[str, str | int]]] = {}
        self.epochs: dict[str, int] = {}
        self.erasure_ids: dict[str, str | None] = {}
        self._work: list[dict[str, Any]] = []
        self._work_id = 0

    async def capture_epoch(self, chat: ChatRef) -> int:
        return self.epochs.setdefault(chat.key, 0)

    async def erasure_active(self, chat: ChatRef) -> bool:
        return self.erasure_ids.get(chat.key) is not None

    def _add_work(self, chat: ChatRef, memory_id: int | None, op: str) -> None:
        self._work_id += 1
        self._work.append(
            {
                "id": self._work_id,
                "chat_key": chat.key,
                "memory_id": memory_id,
                "op": op,
            }
        )

    async def index_work(
        self, chat: ChatRef | None = None, *, due_only: bool = True
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        for item in self._work:
            item.setdefault("attempts", 0)
            item.setdefault("next_attempt_at", now)
        return [
            dict(item)
            for item in self._work
            if chat is None or item["chat_key"] == chat.key
            if not due_only or item.get("attempts", 0) < 8
            if not due_only or item["next_attempt_at"] <= now
        ]

    async def finish_index_work(self, work_id: int) -> None:
        self._work = [item for item in self._work if item["id"] != work_id]

    async def finish_index_work_for(self, chat: ChatRef, memory_id: int | None, op: str) -> None:
        self._work = [
            item
            for item in self._work
            if not (
                item["chat_key"] == chat.key and item["memory_id"] == memory_id and item["op"] == op
            )
        ]

    async def count_index_work(self, chat: ChatRef) -> int:
        return sum(item["chat_key"] == chat.key for item in self._work)

    async def fail_index_work(self, work_id: int) -> None:
        item = next((item for item in self._work if item["id"] == work_id), None)
        if item is not None:
            item["attempts"] = item.get("attempts", 0) + 1
            item["next_attempt_at"] = datetime.now(UTC) + timedelta(
                minutes=min(60, 2 ** min(item["attempts"], 6))
            )

    async def bump_epoch(self, chat: ChatRef, erasure_id: str | None = None) -> int:
        value = self.epochs.get(chat.key, 0) + 1
        self.epochs[chat.key] = value
        if erasure_id is not None:
            self.erasure_ids[chat.key] = erasure_id
        return value

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
            "provenance": "legacy_unknown",
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
        epoch: int | None = None,
        source_ids: list[tuple[str, str]] | None = None,
        provenance: str = "none",
        source_lookup=None,
        policy_version: int = 1,
        index_enabled: bool = True,
    ) -> dict[str, Any]:
        if self.erasure_ids.get(chat.key):
            return {"status": "erasure"}
        if epoch is not None and self.epochs.setdefault(chat.key, 0) != epoch:
            return {"status": "epoch"}
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
            old_sources = self.sources.pop(replaces, [])
            row = await self.insert(
                chat,
                text,
                source=source,
                pinned=bool(old["pinned"] or pinned),
                created_by=created_by,
                subject_user=new_subject,
            )
            if not await self._attach_sources(
                chat, row, source_ids or [], provenance, source_lookup, source, policy_version
            ):
                self.rows.pop(int(row["id"]), None)
                self.sources.pop(int(row["id"]), None)
                self.rows[replaces] = old
                if old_sources:
                    self.sources[replaces] = old_sources
                return {"status": "source"}
            if index_enabled:
                self._add_work(chat, replaces, "delete_doc")
            if index_enabled:
                self._add_work(chat, int(row["id"]), "verify_index")
            return {
                "status": "replaced",
                "row": row,
                "old": old,
                "sources_capped": row.get("sources_capped", False),
            }
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
            if not await self._attach_sources(
                chat,
                exact,
                source_ids or [],
                provenance,
                source_lookup,
                source,
                policy_version,
                preserve_existing=True,
            ):
                return {"status": "source"}
            return {
                "status": "exact",
                "row": exact,
                "sources_capped": exact.get("sources_capped", False),
            }
        if exact_rows and (source == "explicit" or subject_user):
            if not await self._attach_sources(
                chat,
                exact_rows[0],
                source_ids or [],
                provenance,
                source_lookup,
                source,
                policy_version,
                preserve_existing=True,
            ):
                return {"status": "source"}
            return {
                "status": "exact",
                "row": exact_rows[0],
                "sources_capped": exact_rows[0].get("sources_capped", False),
            }
        if exact_rows and source == "auto":
            if not await self._attach_sources(
                chat,
                exact_rows[0],
                source_ids or [],
                provenance,
                source_lookup,
                source,
                policy_version,
                preserve_existing=True,
            ):
                return {"status": "source"}
            return {
                "status": "exact",
                "row": exact_rows[0],
                "sources_capped": exact_rows[0].get("sources_capped", False),
            }
        elif source == "explicit" and near:
            row = await self.insert(
                chat,
                text,
                source=source,
                pinned=pinned,
                created_by=created_by,
                subject_user=subject_user,
            )
            if not await self._attach_sources(
                chat, row, source_ids or [], provenance, source_lookup, source, policy_version
            ):
                self.rows.pop(int(row["id"]), None)
                return {"status": "source"}
            if index_enabled:
                self._add_work(chat, int(row["id"]), "verify_index")
            return {
                "status": "similar",
                "row": row,
                "similar": near,
                "sources_capped": row.get("sources_capped", False),
            }
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
        if not await self._attach_sources(
            chat, row, source_ids or [], provenance, source_lookup, source, policy_version
        ):
            self.rows.pop(int(row["id"]), None)
            self.sources.pop(int(row["id"]), None)
            return {"status": "source"}
        if index_enabled:
            self._add_work(chat, int(row["id"]), "verify_index")
        return {
            "status": "saved",
            "row": row,
            "sources_capped": row.get("sources_capped", False),
        }

    async def _attach_sources(
        self,
        chat,
        row,
        sources,
        provenance,
        source_lookup,
        source,
        policy_version,
        *,
        preserve_existing=False,
    ):
        if source_lookup is None:
            if source == "auto" and any(role == "statement" for _message_id, role in sources):
                return False
            row["provenance"] = (
                row.get("provenance", provenance)
                if preserve_existing and not sources
                else "none"
                if sources
                else provenance
            )
            row["sources_capped"] = False
            stored = self.rows.get(int(row["id"]))
            if stored is not None:
                stored["provenance"] = row["provenance"]
            return True
        resolved_sources = []
        for message_id, role in sources:
            human = await source_lookup(chat, message_id)
            if human is None or human[1]:
                if source == "auto" and role == "statement":
                    return False
                continue
            resolved_sources.append((message_id, role, human[0]))
        linked = self.sources.setdefault(int(row["id"]), [])
        capped = False
        for message_id, role, author_key in resolved_sources:
            existing = next(
                (item for item in linked if item["source_message_id"] == message_id), None
            )
            if existing:
                continue
            if len(linked) >= 32:
                capped = True
                continue
            linked.append(
                {
                    "source_message_id": message_id,
                    "source_author_key": author_key,
                    "source_role": role,
                    "policy_version": policy_version,
                }
            )
        if linked:
            row["provenance"] = (
                "known" if any(s["source_role"] == "statement" for s in linked) else "request_only"
            )
        else:
            row["provenance"] = (
                row.get("provenance", provenance)
                if preserve_existing
                else "none"
                if sources
                else provenance
            )
        stored = self.rows.get(int(row["id"]))
        if stored is not None:
            stored["provenance"] = row["provenance"]
        row["sources_capped"] = capped
        return True

    async def delete(
        self, chat: ChatRef, memory_id: int, *, index_enabled: bool = True
    ) -> dict[str, Any] | None:
        row = await self.get(chat, memory_id)
        if row:
            self.rows.pop(memory_id)
            self.sources.pop(memory_id, None)
            self.epochs[chat.key] = self.epochs.get(chat.key, 0) + 1
            if index_enabled:
                self._add_work(chat, memory_id, "delete_doc")
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

    async def purge_user(
        self, chat: ChatRef, user: UserRef, *, index_enabled: bool = True
    ) -> list[int]:
        ids: list[int] = []
        for memory_id, row in list(self.rows.items()):
            if row["chat_key"] != chat.key:
                continue
            links = self.sources.get(memory_id, [])
            authored = [link for link in links if link["source_author_key"] == user.key]
            must_delete = (
                row.get("subject_user_key") == user.key
                or (
                    row["source"] == "auto"
                    and row["created_by"] == user.key
                    and not any(
                        link["source_role"] == "statement" and link["source_author_key"] != user.key
                        for link in links
                    )
                )
                or any(link["source_role"] == "context" for link in authored)
            )
            has_statement = any(link["source_role"] == "statement" for link in authored)
            has_other_statement = any(
                link["source_role"] == "statement" and link["source_author_key"] != user.key
                for link in links
            )
            has_request = any(link["source_role"] == "request" for link in authored)
            if must_delete or ((has_statement or has_request) and not has_other_statement):
                ids.append(memory_id)
                self.rows.pop(memory_id, None)
                self.sources.pop(memory_id, None)
            elif authored:
                self.sources[memory_id] = [link for link in links if link not in authored]
        if index_enabled:
            for memory_id in ids:
                self._add_work(chat, memory_id, "delete_doc")
        return ids

    async def purge_chat(self, chat: ChatRef, *, index_enabled: bool = True) -> list[int]:
        ids = [i for i, r in self.rows.items() if r["chat_key"] == chat.key]
        for i in ids:
            self.rows.pop(i)
            self.sources.pop(i, None)
        self._work = [
            item
            for item in self._work
            if item["chat_key"] != chat.key or item["op"] == "verify_index"
        ]
        if index_enabled:
            self._add_work(chat, None, "delete_chat")
        return ids


class PostgresMemoryRepo:
    """Persist facts with mandatory chat predicates on all row access."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def capture_epoch(self, chat: ChatRef) -> int:
        await self.db.execute(
            "INSERT INTO memory_control (chat_key) VALUES ($1) ON CONFLICT DO NOTHING", chat.key
        )
        return int(
            await self.db.fetchval("SELECT epoch FROM memory_control WHERE chat_key=$1", chat.key)
        )

    async def erasure_active(self, chat: ChatRef) -> bool:
        return bool(
            await self.db.fetchval(
                "SELECT erasure_id IS NOT NULL FROM memory_control WHERE chat_key=$1", chat.key
            )
        )

    async def index_work(
        self, chat: ChatRef | None = None, *, due_only: bool = True
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT * FROM memory_index_work WHERE ($1::text IS NULL OR chat_key=$1) "
            "AND (NOT $2::boolean OR attempts<8) "
            "AND (NOT $2::boolean OR next_attempt_at<=now()) "
            "ORDER BY id LIMIT 100",
            chat.key if chat else None,
            due_only,
        )
        return [dict(row) for row in rows]

    async def finish_index_work(self, work_id: int) -> None:
        await self.db.execute("DELETE FROM memory_index_work WHERE id=$1", work_id)

    async def finish_index_work_for(self, chat: ChatRef, memory_id: int | None, op: str) -> None:
        await self.db.execute(
            "DELETE FROM memory_index_work WHERE chat_key=$1 "
            "AND memory_id IS NOT DISTINCT FROM $2 AND op=$3",
            chat.key,
            memory_id,
            op,
        )

    async def count_index_work(self, chat: ChatRef) -> int:
        return int(
            await self.db.fetchval(
                "SELECT count(*) FROM memory_index_work WHERE chat_key=$1", chat.key
            )
        )

    async def fail_index_work(self, work_id: int) -> None:
        await self.db.execute(
            "UPDATE memory_index_work SET attempts=attempts+1, "
            "next_attempt_at=now()+LEAST(60, 2^LEAST(attempts,6)) * interval '1 minute' "
            "WHERE id=$1",
            work_id,
        )

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
        epoch: int | None = None,
        source_ids: list[tuple[str, str]] | None = None,
        provenance: str = "none",
        policy_version: int = 1,
        index_enabled: bool = True,
    ) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            await tx.execute(
                "INSERT INTO memory_control (chat_key) VALUES ($1) ON CONFLICT DO NOTHING",
                chat.key,
            )
            control = await tx.fetchrow(
                "SELECT epoch, erasure_id FROM memory_control WHERE chat_key=$1 FOR UPDATE",
                chat.key,
            )
            if control["erasure_id"] is not None:
                return {"status": "erasure"}
            if epoch is not None and int(control["epoch"]) != epoch:
                return {"status": "epoch"}

            valid_sources: list[tuple[str, str, str]] = []
            for message_id, role in source_ids or []:
                source_row = await human_source(tx, chat, message_id)
                row = {"user_key": source_row[0], "is_bot": source_row[1]} if source_row else None
                if row is None or row["is_bot"]:
                    if source == "auto" and role == "statement":
                        return {"status": "source"}
                    continue
                valid_sources.append((message_id, row["user_key"], role))

            async def complete(status, row, **extra):
                row = dict(row)
                added = 0
                capped = False
                if row.get("id") is not None:
                    linked_count = int(
                        await tx.fetchval(
                            "SELECT count(*) FROM memory_sources "
                            "WHERE chat_key=$1 AND memory_id=$2",
                            chat.key,
                            row["id"],
                        )
                    )
                    for message_id, author_key, role in valid_sources:
                        exists = await tx.fetchval(
                            "SELECT EXISTS (SELECT 1 FROM memory_sources WHERE chat_key=$1 "
                            "AND memory_id=$2 AND source_message_id=$3)",
                            chat.key,
                            row["id"],
                            message_id,
                        )
                        if exists:
                            continue
                        if linked_count + added >= 32:
                            capped = True
                            continue
                        result = await tx.execute(
                            "INSERT INTO memory_sources (chat_key,memory_id,source_message_id,"
                            "source_author_key,source_role,policy_version) "
                            "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING",
                            chat.key,
                            row["id"],
                            message_id,
                            author_key,
                            role,
                            policy_version,
                        )
                        added += int(result.endswith("1"))
                    count = await tx.fetchval(
                        "SELECT count(*) FROM memory_sources WHERE chat_key=$1 AND memory_id=$2",
                        chat.key,
                        row["id"],
                    )
                    resolved = (
                        "known"
                        if await tx.fetchval(
                            "SELECT EXISTS (SELECT 1 FROM memory_sources WHERE chat_key=$1 "
                            "AND memory_id=$2 AND source_role='statement')",
                            chat.key,
                            row["id"],
                        )
                        else "request_only"
                        if count
                        else row.get("provenance", provenance)
                        if status == "exact" and not count
                        else "none"
                    )
                    await tx.execute(
                        "UPDATE memories SET provenance=$3 WHERE chat_key=$1 AND id=$2",
                        chat.key,
                        row["id"],
                        resolved,
                    )
                    row["provenance"] = resolved
                    if index_enabled and status in {"saved", "replaced", "similar"}:
                        await tx.execute(
                            "INSERT INTO memory_index_work (chat_key,memory_id,op) "
                            "VALUES ($1,$2,'verify_index')",
                            chat.key,
                            row["id"],
                        )
                if capped:
                    extra["sources_capped"] = True
                return {"status": status, "row": row, **extra}

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
                if index_enabled:
                    await tx.execute(
                        "INSERT INTO memory_index_work (chat_key,memory_id,op) "
                        "VALUES ($1,$2,'delete_doc')",
                        chat.key,
                        replaces,
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
                return await complete("replaced", row, old=old)

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
                return await complete("exact", exact)
            if exact_rows and (source == "explicit" or subject_user):
                return await complete("exact", exact_rows[0])
            if exact_rows and source == "auto":
                return await complete("exact", exact_rows[0])
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
                return await complete("similar", row, similar=near)
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
            return await complete("saved", row)

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

    async def delete(
        self, chat: ChatRef, memory_id: int, *, index_enabled: bool = True
    ) -> dict[str, Any] | None:
        async with self.db.transaction() as tx:
            await tx.execute(
                "INSERT INTO memory_control (chat_key) VALUES ($1) ON CONFLICT DO NOTHING",
                chat.key,
            )
            await tx.fetchrow(
                "SELECT chat_key FROM memory_control WHERE chat_key=$1 FOR UPDATE", chat.key
            )
            await tx.execute("UPDATE memory_control SET epoch=epoch+1 WHERE chat_key=$1", chat.key)
            row = await tx.fetchrow(
                "DELETE FROM memories WHERE chat_key=$1 AND id=$2 RETURNING *",
                chat.key,
                memory_id,
            )
            if row and index_enabled:
                await tx.execute(
                    "INSERT INTO memory_index_work (chat_key,memory_id,op) "
                    "VALUES ($1,$2,'delete_doc')",
                    chat.key,
                    memory_id,
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

    async def purge_user(
        self, chat: ChatRef, user: UserRef, *, index_enabled: bool = True
    ) -> list[int]:
        async with self.db.transaction() as tx:
            deleted: list[int] = []
            cursor = 0
            while True:
                rows = await tx.fetch(
                    "SELECT m.* FROM memories m WHERE m.chat_key=$1 AND m.id>$3 AND ("
                    "m.subject_user_key=$2 OR (m.source='auto' AND m.created_by=$2) "
                    "OR EXISTS (SELECT 1 FROM memory_sources s "
                    "WHERE s.chat_key=m.chat_key AND s.memory_id=m.id AND s.source_author_key=$2)) "
                    "ORDER BY m.id LIMIT 500 FOR UPDATE",
                    chat.key,
                    user.key,
                    cursor,
                )
                if not rows:
                    break
                cursor = int(rows[-1]["id"])
                for memory in rows:
                    memory_id = int(memory["id"])
                    links = await tx.fetch(
                        "SELECT source_author_key, source_role FROM memory_sources "
                        "WHERE chat_key=$1 AND memory_id=$2",
                        chat.key,
                        memory_id,
                    )
                    authored = [link for link in links if link["source_author_key"] == user.key]
                    has_other_statement = any(
                        link["source_role"] == "statement" and link["source_author_key"] != user.key
                        for link in links
                    )
                    must_delete = (
                        memory["subject_user_key"] == user.key
                        or (
                            memory["source"] == "auto"
                            and memory["created_by"] == user.key
                            and not has_other_statement
                        )
                        or any(link["source_role"] == "context" for link in authored)
                    )
                    has_statement = any(link["source_role"] == "statement" for link in authored)
                    has_request = any(link["source_role"] == "request" for link in authored)
                    if must_delete or ((has_statement or has_request) and not has_other_statement):
                        await tx.execute(
                            "DELETE FROM memories WHERE chat_key=$1 AND id=$2", chat.key, memory_id
                        )
                        if index_enabled:
                            await tx.execute(
                                "INSERT INTO memory_index_work (chat_key,memory_id,op) "
                                "VALUES ($1,$2,'delete_doc')",
                                chat.key,
                                memory_id,
                            )
                        deleted.append(memory_id)
                    else:
                        await tx.execute(
                            "DELETE FROM memory_sources WHERE chat_key=$1 AND memory_id=$2 "
                            "AND source_author_key=$3",
                            chat.key,
                            memory_id,
                            user.key,
                        )
            return deleted

    async def purge_chat(self, chat: ChatRef, *, index_enabled: bool = True) -> list[int]:
        async with self.db.transaction() as tx:
            rows = await tx.fetch("DELETE FROM memories WHERE chat_key=$1 RETURNING id", chat.key)
            await tx.execute(
                "DELETE FROM memory_index_work WHERE chat_key=$1 AND op='delete_doc'", chat.key
            )
            if index_enabled:
                await tx.execute(
                    "INSERT INTO memory_index_work (chat_key,op) VALUES ($1,'delete_chat')",
                    chat.key,
                )
        return [int(row["id"]) for row in rows]


def _words(text: str) -> list[str]:
    return re.findall(r"[\w@-]+", text.casefold())


def _query_words(text: str) -> list[str]:
    return re.findall(r"[\w@%\\-]+", text.casefold())
