"""Memory and Postgres repositories for durable research work."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import asyncpg
from chatcore.events import ChatRef, UserRef
from chatcore.store.pg import PgDatabase


@dataclass(frozen=True, slots=True)
class ResearchJob:
    id: UUID
    chat: ChatRef
    is_dm: bool
    thread_id: str | None
    owner: UserRef
    topic: str
    seed_urls: tuple[str, ...]
    status: str
    attempts: int
    tokens_used: int
    lease_token: UUID | None
    lease_until: datetime | None
    delivery_state: str | None
    report_message_id: str | None
    error_class: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ResearchJobs(Protocol):
    async def create(
        self,
        chat: ChatRef,
        user: UserRef,
        thread_id: str | None,
        topic: str,
        seed_urls: tuple[str, ...],
        *,
        is_dm: bool,
    ) -> ResearchJob | None: ...
    async def active_for(self, owner_key: str) -> ResearchJob | None: ...
    async def list_for(
        self, chat_key: str, owner_key: str, limit: int = 5
    ) -> list[ResearchJob]: ...
    async def cancel(self, chat_key: str, owner_key: str) -> ResearchJob | None: ...
    async def claim(self, now: datetime, lease_s: int, max_attempts: int) -> ResearchJob | None: ...
    async def still_owned(self, job: ResearchJob) -> bool: ...
    async def add_tokens(self, job: ResearchJob, n: int) -> None: ...
    async def release(self, job: ResearchJob) -> bool: ...
    async def reserve_delivery(self, job: ResearchJob) -> bool: ...
    async def finish(
        self,
        job: ResearchJob,
        *,
        status: str,
        delivery_state: str | None,
        message_id: str | None = None,
        error_class: str | None = None,
    ) -> bool: ...
    async def purge_user(self, chat_key: str, owner_key: str) -> int: ...
    async def purge_chat(self, chat_key: str) -> int: ...
    async def purge_older_than(self, cutoff: datetime) -> int: ...


def _from_row(row) -> ResearchJob:
    urls = row["seed_urls"]
    if isinstance(urls, str):
        urls = json.loads(urls)
    return ResearchJob(
        row["id"],
        ChatRef.parse(row["chat_key"]),
        row["is_dm"],
        row["thread_id"],
        UserRef.parse(row["owner_key"]),
        row["topic"],
        tuple(urls),
        row["status"],
        row["attempts"],
        row["tokens_used"],
        row["lease_token"],
        row["lease_until"],
        row["delivery_state"],
        row["report_message_id"],
        row["error_class"],
        row["created_at"],
        row["started_at"],
        row["finished_at"],
    )


class MemoryResearchJobs:
    def __init__(self, clock=lambda: datetime.now(UTC)) -> None:
        self.clock = clock
        self.rows: dict[UUID, ResearchJob] = {}

    async def create(self, chat, user, thread_id, topic, seed_urls, *, is_dm):
        if await self.active_for(user.key):
            return None
        job = ResearchJob(
            uuid4(),
            chat,
            is_dm,
            thread_id,
            user,
            topic,
            tuple(seed_urls),
            "queued",
            0,
            0,
            None,
            None,
            None,
            None,
            None,
            self.clock(),
            None,
            None,
        )
        self.rows[job.id] = job
        return job

    async def active_for(self, owner_key):
        return next(
            (
                job
                for job in self.rows.values()
                if job.owner.key == owner_key and job.status in {"queued", "running"}
            ),
            None,
        )

    async def list_for(self, chat_key, owner_key, limit=5):
        return sorted(
            (j for j in self.rows.values() if j.chat.key == chat_key and j.owner.key == owner_key),
            key=lambda j: j.created_at,
            reverse=True,
        )[:limit]

    async def cancel(self, chat_key, owner_key):
        job = next(
            (
                j
                for j in self.rows.values()
                if j.chat.key == chat_key
                and j.owner.key == owner_key
                and j.status in {"queued", "running"}
            ),
            None,
        )
        if job is None:
            return None
        updated = replace(
            job, status="cancelled", finished_at=self.clock(), lease_token=None, lease_until=None
        )
        self.rows[job.id] = updated
        return updated

    async def claim(self, now, lease_s, max_attempts):
        candidates = sorted(
            (
                j
                for j in self.rows.values()
                if j.status == "queued"
                or (j.status == "running" and j.lease_until and j.lease_until < now)
            ),
            key=lambda j: j.created_at,
        )
        for job in candidates:
            if job.status == "running" and job.delivery_state == "reserved":
                self.rows[job.id] = replace(
                    job,
                    status="completed",
                    delivery_state="unknown",
                    finished_at=now,
                    lease_token=None,
                    lease_until=None,
                )
                continue
            if job.attempts >= max_attempts:
                failed = replace(
                    job,
                    status="failed",
                    error_class="AttemptsExhausted",
                    finished_at=now,
                    lease_token=None,
                    lease_until=None,
                )
                self.rows[job.id] = failed
                return failed
            claimed = replace(
                job,
                status="running",
                attempts=job.attempts + 1,
                lease_token=uuid4(),
                lease_until=now + timedelta(seconds=lease_s),
                started_at=job.started_at or now,
            )
            self.rows[job.id] = claimed
            return claimed
        return None

    async def still_owned(self, job):
        current = self.rows.get(job.id)
        return bool(
            current and current.status == "running" and current.lease_token == job.lease_token
        )

    async def add_tokens(self, job, n):
        if await self.still_owned(job):
            current = self.rows[job.id]
            self.rows[job.id] = replace(current, tokens_used=current.tokens_used + max(0, n))

    async def release(self, job):
        if not await self.still_owned(job):
            return False
        current = self.rows[job.id]
        self.rows[job.id] = replace(
            current,
            status="queued",
            attempts=max(0, current.attempts - 1),
            lease_token=None,
            lease_until=None,
        )
        return True

    async def reserve_delivery(self, job):
        if not await self.still_owned(job):
            return False
        current = self.rows[job.id]
        self.rows[job.id] = replace(current, delivery_state="reserved")
        return True

    async def finish(self, job, *, status, delivery_state, message_id=None, error_class=None):
        if not await self.still_owned(job):
            return False
        current = self.rows[job.id]
        self.rows[job.id] = replace(
            current,
            status=status,
            delivery_state=delivery_state,
            report_message_id=message_id,
            error_class=error_class,
            finished_at=self.clock(),
            lease_token=None,
            lease_until=None,
        )
        return True

    async def purge_user(self, chat_key, owner_key):
        ids = [
            key
            for key, job in self.rows.items()
            if job.chat.key == chat_key and job.owner.key == owner_key
        ]
        for key in ids:
            del self.rows[key]
        return len(ids)

    async def purge_chat(self, chat_key):
        ids = [key for key, job in self.rows.items() if job.chat.key == chat_key]
        for key in ids:
            del self.rows[key]
        return len(ids)

    async def purge_older_than(self, cutoff):
        ids = [
            key
            for key, job in self.rows.items()
            if job.status not in {"queued", "running"}
            and job.finished_at
            and job.finished_at < cutoff
        ]
        for key in ids:
            del self.rows[key]
        return len(ids)


class PgResearchJobs:
    def __init__(self, db: PgDatabase, clock=lambda: datetime.now(UTC)) -> None:
        self.db, self.clock = db, clock

    async def create(self, chat, user, thread_id, topic, seed_urls, *, is_dm):
        now = self.clock()
        try:
            row = await self.db.fetchrow(
                "INSERT INTO research_jobs "
                "(id,chat_key,is_dm,thread_id,owner_key,topic,seed_urls,status,created_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,'queued',$8) RETURNING *",
                uuid4(),
                chat.key,
                is_dm,
                thread_id,
                user.key,
                topic,
                json.dumps(list(seed_urls)),
                now,
            )
        except asyncpg.UniqueViolationError:
            return None
        return _from_row(row)

    async def active_for(self, owner_key):
        row = await self.db.fetchrow(
            "SELECT * FROM research_jobs WHERE owner_key=$1 "
            "AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
            owner_key,
        )
        return _from_row(row) if row else None

    async def list_for(self, chat_key, owner_key, limit=5):
        rows = await self.db.fetch(
            "SELECT * FROM research_jobs WHERE chat_key=$1 AND owner_key=$2 "
            "ORDER BY created_at DESC LIMIT $3",
            chat_key,
            owner_key,
            limit,
        )
        return [_from_row(row) for row in rows]

    async def cancel(self, chat_key, owner_key):
        row = await self.db.fetchrow(
            "UPDATE research_jobs SET status='cancelled',finished_at=$3,lease_token=NULL,"
            "lease_until=NULL WHERE id=(SELECT id FROM research_jobs WHERE chat_key=$1 "
            "AND owner_key=$2 AND status IN ('queued','running') "
            "ORDER BY created_at DESC LIMIT 1) AND status IN ('queued','running') RETURNING *",
            chat_key,
            owner_key,
            self.clock(),
        )
        return _from_row(row) if row else None

    async def claim(self, now, lease_s, max_attempts):
        async with self.db.transaction() as tx:
            row = await tx.fetchrow(
                "SELECT * FROM research_jobs WHERE status='queued' OR "
                "(status='running' AND lease_until < $1) ORDER BY created_at "
                "LIMIT 1 FOR UPDATE SKIP LOCKED",
                now,
            )
            if row is None:
                return None
            job = _from_row(row)
            if job.status == "running" and job.delivery_state == "reserved":
                await tx.execute(
                    "UPDATE research_jobs SET status='completed',delivery_state='unknown',"
                    "finished_at=$2,lease_token=NULL,lease_until=NULL "
                    "WHERE id=$1 AND lease_token=$3",
                    job.id,
                    now,
                    job.lease_token,
                )
                return None
            if job.attempts >= max_attempts:
                failed = await tx.fetchrow(
                    "UPDATE research_jobs SET status='failed',error_class='AttemptsExhausted',"
                    "finished_at=$2,lease_token=NULL,lease_until=NULL WHERE id=$1 "
                    "AND (lease_token IS NOT DISTINCT FROM $3) RETURNING *",
                    job.id,
                    now,
                    job.lease_token,
                )
                return _from_row(failed)
            token = uuid4()
            claimed = await tx.fetchrow(
                "UPDATE research_jobs SET status='running',attempts=attempts+1,"
                "lease_token=$2,lease_until=$3,started_at=COALESCE(started_at,$4) "
                "WHERE id=$1 RETURNING *",
                job.id,
                token,
                now + timedelta(seconds=lease_s),
                now,
            )
            return _from_row(claimed)

    async def still_owned(self, job):
        return bool(
            await self.db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM research_jobs WHERE id=$1 AND lease_token=$2 "
                "AND status='running')",
                job.id,
                job.lease_token,
            )
        )

    async def add_tokens(self, job, n):
        await self.db.execute(
            "UPDATE research_jobs SET tokens_used=tokens_used+$3 WHERE id=$1 "
            "AND lease_token=$2 AND status='running'",
            job.id,
            job.lease_token,
            max(0, n),
        )

    async def release(self, job):
        status = await self.db.execute(
            "UPDATE research_jobs SET status='queued',attempts=GREATEST(0,attempts-1),"
            "lease_token=NULL,lease_until=NULL WHERE id=$1 AND lease_token=$2 AND status='running'",
            job.id,
            job.lease_token,
        )
        return status.endswith(" 1")

    async def reserve_delivery(self, job):
        status = await self.db.execute(
            "UPDATE research_jobs SET delivery_state='reserved' WHERE id=$1 "
            "AND lease_token=$2 AND status='running'",
            job.id,
            job.lease_token,
        )
        return status.endswith(" 1")

    async def finish(self, job, *, status, delivery_state, message_id=None, error_class=None):
        result = await self.db.execute(
            "UPDATE research_jobs SET status=$3,delivery_state=$4,report_message_id=$5,"
            "error_class=$6,finished_at=$7,lease_token=NULL,lease_until=NULL "
            "WHERE id=$1 AND lease_token=$2 AND status='running'",
            job.id,
            job.lease_token,
            status,
            delivery_state,
            message_id,
            error_class,
            self.clock(),
        )
        return result.endswith(" 1")

    async def purge_user(self, chat_key, owner_key):
        result = await self.db.execute(
            "DELETE FROM research_jobs WHERE chat_key=$1 AND owner_key=$2", chat_key, owner_key
        )
        return int(result.rsplit(" ", 1)[-1])

    async def purge_chat(self, chat_key):
        result = await self.db.execute("DELETE FROM research_jobs WHERE chat_key=$1", chat_key)
        return int(result.rsplit(" ", 1)[-1])

    async def purge_older_than(self, cutoff):
        result = await self.db.execute(
            "DELETE FROM research_jobs WHERE status NOT IN ('queued','running') "
            "AND finished_at < $1",
            cutoff,
        )
        return int(result.rsplit(" ", 1)[-1])
