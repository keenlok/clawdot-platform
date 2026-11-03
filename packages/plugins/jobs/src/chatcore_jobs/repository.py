"""Scoped Postgres repository for job data and durable delivery reservations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from chatcore.store.pg import PgDatabase

from chatcore_jobs.models import Actor, Claim, Job, JobKind, JobStatus, Scope
from chatcore_jobs.settings import GroupLimits, JobsSettings


class JobConflict(ValueError):
    """A job write conflicted with current state, scope, or configured quota."""


def _job(row: Any) -> Job:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError("invalid stored payload")
    kind = JobKind(row["kind"])
    if kind is JobKind.REMINDER:
        if (
            set(payload) != {"text"}
            or not isinstance(payload["text"], str)
            or not payload["text"].strip()
            or len(payload["text"]) > 1000
        ):
            raise ValueError("invalid reminder payload")
    elif set(payload) not in ({"url", "interval_seconds"}, {"url", "interval_seconds", "mode"}):
        raise ValueError("invalid watch payload")
    if kind is JobKind.WATCH:
        url = payload.get("url")
        interval = payload.get("interval_seconds")
        mode = payload.get("mode", "plain")
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError("invalid watch URL")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid watch URL") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 80, 443}
            or not isinstance(mode, str)
            or mode not in {"plain", "rendered"}
            or (mode == "rendered" and parsed.scheme.lower() != "https")
            or type(interval) is not int
            or not 1 <= interval <= 604800
        ):
            raise ValueError("invalid watch payload")
    if row["snapshot"] is not None and len(row["snapshot"]) > 20000:
        raise ValueError("invalid snapshot")
    if row["snapshot_hash"] is not None and len(row["snapshot_hash"]) != 64:
        raise ValueError("invalid snapshot hash")
    return Job(
        id=row["id"],
        scope=Scope(row["chat_key"], row["is_dm"], row["thread_id"]),
        owner=Actor(row["owner_key"]),
        kind=kind,
        status=JobStatus(row["status"]),
        payload=payload,
        created_at=row["created_at"],
        next_run_at=row["next_run_at"],
        occurrence=row["occurrence"],
        revision=row["revision"],
        last_delivery_state=row["last_delivery_state"],
        snapshot=row["snapshot"],
        snapshot_hash=row["snapshot_hash"],
        failure_count=row["failure_count"],
        error_episode=row["error_episode"],
        terminal_at=row.get("terminal_at"),
    )


class JobsRepo:
    def __init__(self, db: PgDatabase, settings: JobsSettings | None = None) -> None:
        if not isinstance(db, PgDatabase):
            raise TypeError("JobsRepo requires PgDatabase")
        self.db = db
        self.settings_config = settings or JobsSettings()

    async def settings(self, scope: Scope) -> dict[str, Any]:
        row = await self.db.fetchrow(
            "SELECT * FROM job_settings WHERE chat_key=$1 AND is_dm=$2",
            scope.chat_key,
            scope.is_dm,
        )
        if row is None:
            return {
                "reminders_enabled": False if not scope.is_dm else self.settings_config.dm_enabled,
                "watches_enabled": False if not scope.is_dm else self.settings_config.dm_enabled,
                "max_jobs_per_user": self.settings_config.max_jobs_per_user,
                "max_jobs_per_chat": self.settings_config.max_jobs_per_chat,
                "minimum_interval_seconds": self.settings_config.minimum_watch_interval_seconds,
                "revision": 0,
            }
        result = dict(row)
        result["max_jobs_per_user"] = min(
            result.get("max_jobs_per_user") or self.settings_config.max_jobs_per_user,
            self.settings_config.max_jobs_per_user,
        )
        result["max_jobs_per_chat"] = min(
            result.get("max_jobs_per_chat") or self.settings_config.max_jobs_per_chat,
            self.settings_config.max_jobs_per_chat,
        )
        result["minimum_interval_seconds"] = max(
            result.get("minimum_interval_seconds")
            or self.settings_config.minimum_watch_interval_seconds,
            self.settings_config.minimum_watch_interval_seconds,
        )
        return result

    async def configure(
        self,
        scope: Scope,
        *,
        kind: JobKind,
        enabled: bool | None = None,
        limits: GroupLimits | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if type(enabled) is not bool and enabled is not None:
            raise ValueError("enabled must be boolean")
        if scope.is_dm:
            raise JobConflict("DM settings are controlled globally")
        if limits:
            if limits.max_jobs_per_user > self.settings_config.max_jobs_per_user:
                raise ValueError("user quota exceeds global ceiling")
            if limits.max_jobs_per_chat > self.settings_config.max_jobs_per_chat:
                raise ValueError("chat quota exceeds global ceiling")
            if (
                limits.minimum_interval_seconds
                < self.settings_config.minimum_watch_interval_seconds
            ):
                raise ValueError("interval below global minimum")
        async with self.db.transaction() as tx:
            await tx.execute(
                "INSERT INTO job_settings (chat_key, is_dm) VALUES ($1, false) "
                "ON CONFLICT DO NOTHING",
                scope.chat_key,
            )
            current = await tx.fetchrow(
                "SELECT * FROM job_settings WHERE chat_key=$1 AND is_dm=false FOR UPDATE",
                scope.chat_key,
            )
            if expected_revision is not None and current["revision"] != expected_revision:
                raise JobConflict("settings changed while awaiting confirmation")
            active_before_disable = 0
            if enabled is False:
                jobs_to_cancel = await tx.fetch(
                    """SELECT id FROM scheduled_jobs WHERE chat_key=$1 AND is_dm=false
                       AND kind=$2 AND status='active' FOR UPDATE""",
                    scope.chat_key,
                    kind.value,
                )
                active_before_disable = len(jobs_to_cancel)
            row = await tx.fetchrow(
                """UPDATE job_settings SET
                   reminders_enabled=CASE WHEN $2='reminder' AND $3::boolean IS NOT NULL
                     THEN $3 ELSE reminders_enabled END,
                   watches_enabled=CASE WHEN $2='watch' AND $3::boolean IS NOT NULL
                     THEN $3 ELSE watches_enabled END,
                   max_jobs_per_user=COALESCE($4,max_jobs_per_user),
                   max_jobs_per_chat=COALESCE($5,max_jobs_per_chat),
                   minimum_interval_seconds=COALESCE($6,minimum_interval_seconds),
                   revision=revision+1,updated_at=now()
                   WHERE chat_key=$1 AND is_dm=false RETURNING *""",
                scope.chat_key,
                kind.value,
                enabled,
                limits.max_jobs_per_user if limits else None,
                limits.max_jobs_per_chat if limits else None,
                limits.minimum_interval_seconds if limits else None,
            )
            if limits:
                # Existing watches that no longer satisfy the newly effective floor
                # are cancelled explicitly; they must not keep polling faster.
                await tx.execute(
                    """UPDATE scheduled_jobs SET status='cancelled',terminal_at=now(),
                       last_delivery_state='interval_floor_raised',lease_token=NULL,
                       lease_until=NULL,revision=revision+1 WHERE chat_key=$1 AND is_dm=false
                       AND kind='watch' AND status='active' AND COALESCE(
                         CASE WHEN jsonb_typeof(payload->'interval_seconds')='number'
                           THEN (payload->>'interval_seconds')::integer END,0) < $2""",
                    scope.chat_key,
                    max(
                        limits.minimum_interval_seconds,
                        self.settings_config.minimum_watch_interval_seconds,
                    ),
                )
            if enabled is False:
                await tx.execute(
                    """UPDATE scheduled_jobs SET status='cancelled', lease_token=NULL,
                       lease_until=NULL, terminal_at=now(), revision=revision+1
                       WHERE chat_key=$1 AND is_dm=false
                       AND kind=$2 AND status='active'""",
                    scope.chat_key,
                    kind.value,
                )
            result = dict(row)
            result["_cancelled_jobs"] = active_before_disable
            return result

    async def count_active(self, scope: Scope, kind: JobKind) -> int:
        if scope.is_dm:
            return 0
        return int(
            await self.db.fetchval(
                """SELECT count(*) FROM scheduled_jobs WHERE chat_key=$1 AND is_dm=false
                   AND kind=$2 AND status='active'""",
                scope.chat_key,
                kind.value,
            )
        )

    async def count_active_rendered_watches(self) -> int:
        return int(
            await self.db.fetchval(
                """SELECT count(*) FROM scheduled_jobs WHERE kind='watch' AND status='active'
                   AND payload->'mode'='\"rendered\"'::jsonb"""
            )
        )

    async def _check_capacity(self, tx, scope, actor, kind, payload, revision) -> None:
        config = await tx.fetchrow(
            "SELECT * FROM job_settings WHERE chat_key=$1 AND is_dm=$2 FOR UPDATE",
            scope.chat_key,
            scope.is_dm,
        )
        if scope.is_dm:
            enabled = self.settings_config.dm_enabled
            user_limit = self.settings_config.max_jobs_per_user
            chat_limit = self.settings_config.max_jobs_per_chat
            current_revision = 0
            minimum_interval = self.settings_config.minimum_watch_interval_seconds
        else:
            enabled = bool(
                config
                and config["reminders_enabled" if kind is JobKind.REMINDER else "watches_enabled"]
            )
            user_limit = (
                min(
                    config["max_jobs_per_user"] or self.settings_config.max_jobs_per_user,
                    self.settings_config.max_jobs_per_user,
                )
                if config
                else self.settings_config.max_jobs_per_user
            )
            chat_limit = (
                min(
                    config["max_jobs_per_chat"] or self.settings_config.max_jobs_per_chat,
                    self.settings_config.max_jobs_per_chat,
                )
                if config
                else self.settings_config.max_jobs_per_chat
            )
            current_revision = config["revision"] if config else 0
            minimum_interval = (
                max(
                    config["minimum_interval_seconds"]
                    or self.settings_config.minimum_watch_interval_seconds,
                    self.settings_config.minimum_watch_interval_seconds,
                )
                if config
                else self.settings_config.minimum_watch_interval_seconds
            )
        if not enabled:
            raise JobConflict("tool disabled")
        if current_revision != revision:
            raise JobConflict("settings changed while awaiting confirmation")
        if kind is JobKind.WATCH:
            interval = payload.get("interval_seconds")
            mode = payload.get("mode", "plain")
            if (
                type(interval) is not int
                or interval < minimum_interval
                or interval > self.settings_config.maximum_watch_interval_seconds
                or not isinstance(mode, str)
                or mode not in {"plain", "rendered"}
                or (
                    mode == "rendered"
                    and interval
                    < max(
                        minimum_interval,
                        self.settings_config.minimum_rendered_watch_interval_seconds,
                    )
                )
            ):
                raise JobConflict("watch interval outside current bounds")
            if mode == "rendered":
                rendered_floor = max(
                    minimum_interval, self.settings_config.minimum_rendered_watch_interval_seconds
                )
                if rendered_floor > self.settings_config.maximum_watch_interval_seconds:
                    raise JobConflict("rendered watch interval window is empty")
                active_rendered = await tx.fetchval(
                    """SELECT count(*) FROM scheduled_jobs WHERE kind='watch'
                       AND status='active' AND payload->'mode'='\"rendered\"'::jsonb"""
                )
                if (
                    self.settings_config.max_rendered_watches_global == 0
                    or active_rendered >= self.settings_config.max_rendered_watches_global
                ):
                    raise JobConflict("rendered watch quota is full")
        active_global = await tx.fetchval(
            "SELECT count(*) FROM scheduled_jobs WHERE status='active'"
        )
        active_chat = await tx.fetchval(
            "SELECT count(*) FROM scheduled_jobs WHERE chat_key=$1 AND is_dm=$2 "
            "AND status='active'",
            scope.chat_key,
            scope.is_dm,
        )
        active_user = await tx.fetchval(
            """SELECT count(*) FROM scheduled_jobs WHERE chat_key=$1 AND is_dm=$2
               AND thread_id IS NOT DISTINCT FROM $4 AND owner_key=$3 AND status='active'""",
            scope.chat_key,
            scope.is_dm,
            actor.user_key,
            scope.thread_id,
        )
        if (
            active_global >= self.settings_config.max_jobs_global
            or active_chat >= chat_limit
            or active_user >= user_limit
        ):
            raise JobConflict("job quota is full")

    async def create(
        self,
        scope: Scope,
        actor: Actor,
        action_id: str,
        kind: JobKind,
        payload: dict[str, Any],
        due_at: datetime,
        *,
        revision: int,
        now: datetime,
    ) -> Job:
        if due_at.tzinfo is None or now.tzinfo is None or not action_id:
            raise ValueError("aware times and action ID are required")
        async with self.db.transaction() as tx:
            await tx.fetchval("SELECT pg_advisory_xact_lock($1)", 0x6A6F625F71756F74)
            await tx.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", scope.chat_key
            )
            existing = await tx.fetchrow(
                "SELECT * FROM scheduled_jobs WHERE chat_key=$1 AND source_action_id=$2",
                scope.chat_key,
                action_id,
            )
            if existing:
                job = _job(existing)
                if job.scope != scope or job.owner != actor or job.kind is not kind:
                    raise JobConflict("action already used")
                return job
            await self._check_capacity(tx, scope, actor, kind, payload, revision)
            ident = uuid4()
            row = await tx.fetchrow(
                """INSERT INTO scheduled_jobs (
                   id,chat_key,is_dm,owner_key,thread_id,kind,status,payload,
                   source_action_id,created_at,next_run_at,revision)
                   VALUES ($1,$2,$3,$4,$5,$6,'active',$7::jsonb,$8,$9,$10,$11) RETURNING *""",
                ident,
                scope.chat_key,
                scope.is_dm,
                actor.user_key,
                scope.thread_id,
                kind.value,
                json.dumps(payload),
                action_id,
                now,
                due_at,
                revision,
            )
            return _job(row)

    async def restore(
        self,
        scope: Scope,
        actor: Actor,
        kind: JobKind,
        job_id: UUID,
        *,
        all_members: bool,
        expected_revision: int,
        revision: int,
        now: datetime,
    ) -> Job | None:
        if now.tzinfo is None:
            raise ValueError("aware time is required")
        async with self.db.transaction() as tx:
            await tx.fetchval("SELECT pg_advisory_xact_lock($1)", 0x6A6F625F71756F74)
            await tx.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", scope.chat_key
            )
            restore_window = self.settings_config.restore_window_seconds
            row = await tx.fetchrow(
                """SELECT * FROM scheduled_jobs WHERE id=$1 AND chat_key=$2 AND is_dm=$3
                   AND thread_id IS NOT DISTINCT FROM $4 AND kind=$5 AND status='cancelled'
                   AND last_delivery_state='cancelled_by_user' AND ($6 OR owner_key=$7)
                   AND revision=$8 AND terminal_at > $9::timestamptz
                   - ($10::integer * interval '1 second')
                   FOR UPDATE""",
                job_id,
                scope.chat_key,
                scope.is_dm,
                scope.thread_id,
                kind.value,
                all_members,
                actor.user_key,
                expected_revision,
                now,
                restore_window,
            )
            if row is None:
                return None
            job = _job(row)
            if kind is JobKind.REMINDER and job.next_run_at <= now:
                raise JobConflict("reminder time passed")
            await self._check_capacity(tx, scope, job.owner, kind, job.payload, revision)
            restored = await tx.fetchrow(
                """UPDATE scheduled_jobs SET status='active',terminal_at=NULL,
                   last_delivery_state=NULL,lease_token=NULL,lease_until=NULL,
                   revision=revision+1,next_run_at=GREATEST(next_run_at,$9::timestamptz)
                   WHERE id=$1 AND chat_key=$2 AND is_dm=$3
                   AND thread_id IS NOT DISTINCT FROM $4 AND kind=$5 AND status='cancelled'
                   AND last_delivery_state='cancelled_by_user' AND ($6 OR owner_key=$7)
                   AND revision=$8 AND (kind <> 'reminder' OR next_run_at > $9::timestamptz)
                   AND terminal_at > $9::timestamptz
                   - ($10::integer * interval '1 second') RETURNING *""",
                job_id,
                scope.chat_key,
                scope.is_dm,
                scope.thread_id,
                kind.value,
                all_members,
                actor.user_key,
                expected_revision,
                now,
                restore_window,
            )
            return _job(restored) if restored else None

    async def list_jobs(
        self, scope: Scope, actor: Actor, kind: JobKind, *, all_members: bool = False
    ) -> list[Job]:
        rows = await self.db.fetch(
            """SELECT * FROM scheduled_jobs WHERE chat_key=$1 AND is_dm=$2
               AND thread_id IS NOT DISTINCT FROM $3 AND kind=$4
               AND ($5 OR owner_key=$6) ORDER BY created_at,id""",
            scope.chat_key,
            scope.is_dm,
            scope.thread_id,
            kind.value,
            all_members,
            actor.user_key,
        )
        jobs = []
        for row in rows:
            try:
                jobs.append(_job(row))
            except (ValueError, TypeError, KeyError):
                continue
        return jobs

    async def get_job(self, scope: Scope, job_id: UUID) -> Job | None:
        row = await self.db.fetchrow(
            """SELECT * FROM scheduled_jobs WHERE id=$1 AND chat_key=$2 AND is_dm=$3
               AND thread_id IS NOT DISTINCT FROM $4""",
            job_id,
            scope.chat_key,
            scope.is_dm,
            scope.thread_id,
        )
        if row is None:
            return None
        try:
            return _job(row)
        except (ValueError, TypeError, KeyError):
            return None

    async def cancel(
        self,
        scope: Scope,
        actor: Actor,
        kind: JobKind,
        job_id: UUID,
        *,
        all_members: bool = False,
        expected_revision: int | None = None,
    ) -> bool:
        row = await self.db.fetchrow(
            """UPDATE scheduled_jobs SET status='cancelled',terminal_at=now(),
               last_delivery_state='cancelled_by_user',
               lease_token=NULL,lease_until=NULL,
               revision=revision+1 WHERE id=$1 AND chat_key=$2 AND is_dm=$3
               AND thread_id IS NOT DISTINCT FROM $4 AND kind=$5 AND status='active'
               AND ($6 OR owner_key=$7) AND ($8::bigint IS NULL OR revision=$8) RETURNING id""",
            job_id,
            scope.chat_key,
            scope.is_dm,
            scope.thread_id,
            kind.value,
            all_members,
            actor.user_key,
            expected_revision,
        )
        return row is not None

    async def claim_due(self, now: datetime, limit: int | None = None) -> list[Claim]:
        if now.tzinfo is None or (limit is not None and type(limit) is not int):
            raise ValueError("aware time and integer limit are required")
        async with self.db.transaction() as tx:
            await tx.fetchval("SELECT pg_advisory_xact_lock($1)", 0x6A6F625F73636864)
            await tx.execute(
                """WITH recovered AS (
                     UPDATE job_deliveries SET status='unknown',finished_at=$1
                     WHERE status='reserved' AND reserved_at + make_interval(
                       secs => $2::double precision) <= $1
                     RETURNING chat_key,job_id,occurrence,notification_kind,terminal_status
                   )
                   UPDATE scheduled_jobs j SET last_delivery_state='unknown',
                     status=CASE WHEN j.kind='reminder' THEN 'failed'
                       ELSE COALESCE(r.terminal_status,j.status) END,
                     terminal_at=CASE WHEN j.kind='reminder' OR r.terminal_status IS NOT NULL
                       THEN $1 ELSE j.terminal_at END,
                     lease_token=NULL,lease_until=NULL
                   FROM recovered r WHERE j.id=r.job_id AND j.chat_key=r.chat_key
                     AND j.occurrence=r.occurrence+1 AND j.status='active'""",
                now,
                self.settings_config.lease_seconds,
            )
            leased = await tx.fetchval(
                """SELECT count(*) FROM scheduled_jobs j WHERE status='active'
                   AND (lease_until > $1 OR EXISTS (
                     SELECT 1 FROM job_deliveries d WHERE d.job_id=j.id
                       AND d.chat_key=j.chat_key AND d.status='reserved'))""",
                now,
            )
            available = max(0, self.settings_config.worker_concurrency - leased)
            claim_limit = min(available, limit if limit is not None else available)
            if not claim_limit:
                return []
            rows = await tx.fetch(
                """SELECT * FROM scheduled_jobs WHERE status='active' AND next_run_at <= $1
                   AND (lease_until IS NULL OR lease_until <= $1)
                   AND NOT EXISTS (SELECT 1 FROM job_deliveries d WHERE d.job_id=scheduled_jobs.id
                     AND d.chat_key=scheduled_jobs.chat_key AND d.status='reserved')
                   ORDER BY next_run_at,id LIMIT $2 FOR UPDATE SKIP LOCKED""",
                now,
                claim_limit,
            )
            claims = []
            for row in rows:
                token = uuid4()
                lease_until = now + timedelta(seconds=self.settings_config.lease_seconds)
                updated = await tx.fetchrow(
                    """UPDATE scheduled_jobs SET lease_token=$2,lease_until=$3
                       WHERE id=$1 AND status='active' RETURNING *""",
                    row["id"],
                    token,
                    lease_until,
                )
                if updated:
                    try:
                        job = _job(updated)
                    except (ValueError, TypeError, KeyError):
                        await tx.execute(
                            """UPDATE scheduled_jobs SET status='failed',
                               terminal_at=$4,last_delivery_state='invalid_payload',lease_token=NULL,lease_until=NULL
                               WHERE id=$1 AND chat_key=$2 AND lease_token=$3""",
                            row["id"],
                            row["chat_key"],
                            token,
                            now,
                        )
                        continue
                    claims.append(Claim(job, token, lease_until))
            return claims

    async def finish_run(
        self,
        claim: Claim,
        *,
        now: datetime,
        next_run_at: datetime | None,
        snapshot: str | None = None,
        snapshot_hash: str | None = None,
        failure_count: int | None = None,
        error_episode: bool | None = None,
        status: JobStatus = JobStatus.ACTIVE,
        delivery_state: str | None = None,
    ) -> bool:
        row = await self.db.fetchrow(
            """UPDATE scheduled_jobs SET status=$5,next_run_at=COALESCE($6,next_run_at),
               snapshot=COALESCE($7,snapshot),snapshot_hash=COALESCE($8,snapshot_hash),
               failure_count=COALESCE($9,failure_count),error_episode=COALESCE($10,error_episode),
               last_delivery_state=COALESCE($11,last_delivery_state),occurrence=occurrence+1,
               terminal_at=CASE WHEN $5='active' THEN terminal_at ELSE $12 END,
               lease_token=NULL,lease_until=NULL
               WHERE id=$1 AND chat_key=$2 AND is_dm=$3 AND lease_token=$4 AND status='active'
               RETURNING id""",
            claim.job.id,
            claim.job.scope.chat_key,
            claim.job.scope.is_dm,
            claim.token,
            status.value,
            next_run_at,
            snapshot,
            snapshot_hash,
            failure_count,
            error_episode,
            delivery_state,
            now,
        )
        return row is not None

    async def reserve_delivery(
        self,
        claim: Claim,
        content: str,
        notification_kind: str,
        *,
        now: datetime,
        next_run_at: datetime | None,
        snapshot: str | None = None,
        snapshot_hash: str | None = None,
        failure_count: int | None = None,
        error_episode: bool | None = None,
        terminal_status: str | None = None,
    ) -> bool:
        content = content[:1500]
        if terminal_status not in {None, "completed", "failed"}:
            raise ValueError("invalid terminal job state")
        async with self.db.transaction() as tx:
            job = await tx.fetchrow(
                """SELECT * FROM scheduled_jobs WHERE id=$1 AND chat_key=$2 AND is_dm=$3
                   AND lease_token=$4 AND status='active' AND lease_until > $5 FOR UPDATE""",
                claim.job.id,
                claim.job.scope.chat_key,
                claim.job.scope.is_dm,
                claim.token,
                now,
            )
            if job is None or job["occurrence"] != claim.job.occurrence:
                return False
            delivery = await tx.fetchrow(
                """INSERT INTO job_deliveries(chat_key,job_id,occurrence,notification_kind,
                   status,terminal_status,content,reserved_at,claim_token)
                   VALUES($1,$2,$3,$4,'reserved',$5,$6,$7,$8)
                   ON CONFLICT(chat_key,job_id,occurrence,notification_kind)
                   DO NOTHING RETURNING job_id""",
                claim.job.scope.chat_key,
                claim.job.id,
                claim.job.occurrence,
                notification_kind,
                terminal_status,
                content,
                now,
                claim.token,
            )
            if delivery is None:
                return False
            await tx.execute(
                """UPDATE scheduled_jobs SET next_run_at=COALESCE($5,next_run_at),
                   snapshot=COALESCE($6,snapshot),snapshot_hash=COALESCE($7,snapshot_hash),
                   failure_count=COALESCE($8,failure_count),error_episode=COALESCE($9,error_episode),
                   last_delivery_state='reserved',occurrence=occurrence+1
                   WHERE id=$1 AND chat_key=$2 AND is_dm=$3 AND lease_token=$4""",
                claim.job.id,
                claim.job.scope.chat_key,
                claim.job.scope.is_dm,
                claim.token,
                next_run_at,
                snapshot,
                snapshot_hash,
                failure_count,
                error_episode,
            )
            return True

    async def delivery_allowed(
        self, claim: Claim, notification_kind: str, *, now: datetime | None = None
    ) -> bool:
        return bool(
            await self.db.fetchval(
                """SELECT EXISTS(SELECT 1 FROM scheduled_jobs j JOIN job_deliveries d
               ON d.job_id=j.id AND d.chat_key=j.chat_key WHERE j.id=$1 AND j.chat_key=$2
               AND j.is_dm=$3 AND j.status='active' AND j.lease_token=$4
               AND j.lease_until > COALESCE($7,now()) AND d.occurrence=$5
               AND d.notification_kind=$6 AND d.status='reserved' AND d.claim_token=$4)""",
                claim.job.id,
                claim.job.scope.chat_key,
                claim.job.scope.is_dm,
                claim.token,
                claim.job.occurrence,
                notification_kind,
                now,
            )
        )

    async def finish_delivery(
        self,
        claim: Claim,
        notification_kind: str,
        *,
        status: str,
        message_id: str | None = None,
        terminal_status: str | None = None,
        now: datetime,
    ) -> bool:
        if status not in {"sent", "unknown", "suppressed"}:
            raise ValueError("invalid delivery outcome")
        if terminal_status not in {None, "completed", "failed"}:
            raise ValueError("invalid terminal job state")
        async with self.db.transaction() as tx:
            row = await tx.fetchrow(
                """UPDATE job_deliveries SET status=$6,platform_message_id=$7,finished_at=$8
                   WHERE chat_key=$1 AND job_id=$2 AND occurrence=$3 AND notification_kind=$4
                   AND status='reserved' AND claim_token=$5 RETURNING job_id""",
                claim.job.scope.chat_key,
                claim.job.id,
                claim.job.occurrence,
                notification_kind,
                claim.token,
                status,
                message_id,
                now,
            )
            if row is None:
                return False
            new_status = (
                "failed"
                if status in {"unknown", "suppressed"} and claim.job.kind is JobKind.REMINDER
                else terminal_status
                if status in {"sent", "unknown", "suppressed"}
                else None
            )
            await tx.execute(
                """UPDATE scheduled_jobs SET status=COALESCE($3,status),last_delivery_state=$4,
                   terminal_at=CASE WHEN $3 IS NOT NULL THEN $6 ELSE terminal_at END,
                   lease_token=NULL,lease_until=NULL
                   WHERE id=$1 AND chat_key=$2 AND status='active' AND lease_token=$5""",
                claim.job.id,
                claim.job.scope.chat_key,
                new_status,
                status,
                claim.token,
                now,
            )
            return True

    async def finish_claim_reservations(self, claim: Claim, *, status: str, now: datetime) -> int:
        """Resolve any reservation left by a worker exception or cancellation."""
        if status not in {"unknown", "suppressed"}:
            raise ValueError("invalid abandoned delivery outcome")
        async with self.db.transaction() as tx:
            rows = await tx.fetch(
                """UPDATE job_deliveries SET status=$5,finished_at=$6
                   WHERE chat_key=$1 AND job_id=$2 AND occurrence=$3
                     AND claim_token=$4 AND status='reserved'
                   RETURNING terminal_status""",
                claim.job.scope.chat_key,
                claim.job.id,
                claim.job.occurrence,
                claim.token,
                status,
                now,
            )
            if not rows:
                return 0
            terminal_states = {row["terminal_status"] for row in rows if row["terminal_status"]}
            if claim.job.kind is JobKind.REMINDER and status in {"unknown", "suppressed"}:
                new_status = "failed"
            elif "failed" in terminal_states:
                new_status = "failed"
            elif terminal_states:
                new_status = next(iter(terminal_states))
            else:
                new_status = None
            await tx.execute(
                """UPDATE scheduled_jobs SET status=COALESCE($3,status),
                   terminal_at=CASE WHEN $3 IS NOT NULL THEN $4 ELSE terminal_at END,
                   last_delivery_state=$5,lease_token=NULL,lease_until=NULL
                   WHERE id=$1 AND chat_key=$2 AND status='active' AND lease_token=$6""",
                claim.job.id,
                claim.job.scope.chat_key,
                new_status,
                now,
                status,
                claim.token,
            )
            return len(rows)

    async def release_claim(self, claim: Claim) -> bool:
        row = await self.db.fetchrow(
            """UPDATE scheduled_jobs SET lease_token=NULL,lease_until=NULL
               WHERE id=$1 AND chat_key=$2 AND is_dm=$3 AND status='active' AND lease_token=$4
               RETURNING id""",
            claim.job.id,
            claim.job.scope.chat_key,
            claim.job.scope.is_dm,
            claim.token,
        )
        return row is not None

    async def purge_user(self, chat_key: str, user_key: str) -> None:
        await self.db.execute(
            "DELETE FROM scheduled_jobs WHERE chat_key=$1 AND owner_key=$2", chat_key, user_key
        )

    async def purge_chat(self, chat_key: str) -> None:
        async with self.db.transaction() as tx:
            await tx.execute("DELETE FROM scheduled_jobs WHERE chat_key=$1", chat_key)
            await tx.execute("DELETE FROM job_settings WHERE chat_key=$1", chat_key)

    async def purge_older_than(self, cutoff: datetime, *, now: datetime | None = None) -> None:
        from datetime import UTC

        now = now or datetime.now(UTC)
        retention_cutoff = max(cutoff, now - timedelta(days=self.settings_config.retention_days))
        async with self.db.transaction() as tx:
            await tx.execute(
                """DELETE FROM job_deliveries d USING scheduled_jobs j
                   WHERE d.job_id=j.id AND d.chat_key=j.chat_key AND j.status='active'
                     AND j.kind='watch' AND d.status <> 'reserved'
                     AND d.occurrence < j.occurrence-1
                     AND COALESCE(d.finished_at,d.reserved_at) < $1""",
                retention_cutoff,
            )
            await tx.execute(
                """DELETE FROM scheduled_jobs WHERE status <> 'active'
                   AND COALESCE(terminal_at,created_at) < $1""",
                retention_cutoff,
            )
