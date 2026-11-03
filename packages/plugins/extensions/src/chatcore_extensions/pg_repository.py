from __future__ import annotations

# SQL statements are split for driver parameter alignment.
# ruff: noqa: E501
import json
import secrets
from datetime import datetime
from typing import Any

from chatcore.events import ChatRef, UserRef
from chatcore.store.pg import PgDatabase

from chatcore_extensions.models import Artifact, ExampleCase, RunResult
from chatcore_extensions.repository import (
    ExtensionActivation,
    ExtensionEvidence,
    ExtensionRequest,
    ExtensionRevision,
)


class PostgresExtensionRepo:
    def __init__(self, db: PgDatabase):
        self.db = db

    async def create_request(
        self, *, chat: ChatRef, actor: UserRef, description: str, examples: tuple[ExampleCase, ...]
    ) -> str:
        async with self.db.transaction() as db:
            await db.fetchval("SELECT pg_advisory_xact_lock(hashtext($1))", chat.key)
            count = await db.fetchval(
                "SELECT count(*) FROM extension_requests WHERE chat_key=$1 AND state='requested'",
                chat.key,
            )
            if count >= 100:
                raise ValueError("scope proposal limit reached")
            item_id = secrets.token_hex(12)
            await db.execute(
                "INSERT INTO extension_requests(chat_key,id,author_key,description,examples,state) "
                "VALUES($1,$2,$3,$4,$5::jsonb,'requested')",
                chat.key,
                item_id,
                actor.key,
                description,
                json.dumps([{"arguments": c.arguments, "expected": c.expected} for c in examples]),
            )
            return item_id

    async def get_request(self, *, chat: ChatRef, request_id: str) -> ExtensionRequest | None:
        row = await self.db.fetchrow(
            "SELECT * FROM extension_requests WHERE chat_key=$1 AND id=$2", chat.key, request_id
        )
        if not row:
            return None
        return ExtensionRequest(
            row["id"],
            row["chat_key"],
            row["author_key"],
            row["description"],
            _cases(row["examples"]),
            row["state"],
            row["created_at"],
        )

    async def claim_build(self, *, chat: ChatRef, request_id: str, actor: UserRef) -> bool:
        async with self.db.transaction() as db:
            row = await db.fetchrow(
                "SELECT id FROM extension_requests WHERE chat_key=$1 AND id=$2 AND state='requested' FOR UPDATE",
                chat.key,
                request_id,
            )
            if row is None:
                return False
            count = await db.fetchval(
                "INSERT INTO extension_quota(chat_key,utc_day,builds) "
                "VALUES($1,(now() AT TIME ZONE 'UTC')::date,1) "
                "ON CONFLICT(chat_key,utc_day) DO UPDATE "
                "SET builds=extension_quota.builds+1 WHERE extension_quota.builds<5 "
                "RETURNING builds",
                chat.key,
            )
            if count is None:
                return False
            changed = await db.execute(
                "UPDATE extension_requests SET state='building' WHERE chat_key=$1 AND id=$2 AND state='requested'",
                chat.key,
                request_id,
            )
            return changed.endswith("1")

    async def fail_build(self, *, chat: ChatRef, request_id: str) -> None:
        await self.db.execute(
            "UPDATE extension_requests SET state='failed' WHERE chat_key=$1 AND id=$2 AND state='building'",
            chat.key,
            request_id,
        )

    async def save_revision(
        self, *, chat: ChatRef, request_id: str, actor: UserRef, artifact: Artifact
    ) -> str:
        async with self.db.transaction() as db:
            row = await db.fetchrow(
                "SELECT state FROM extension_requests WHERE chat_key=$1 AND id=$2 FOR UPDATE",
                chat.key,
                request_id,
            )
            if row is None or row["state"] != "building":
                raise ValueError("request unavailable")
            item_id = secrets.token_hex(12)
            await db.execute(
                "INSERT INTO extension_revisions(chat_key,id,request_id,author_key,artifact,digest) "
                "VALUES($1,$2,$3,$4,$5::jsonb,$6)",
                chat.key,
                item_id,
                request_id,
                actor.key,
                artifact.to_json(),
                artifact.digest,
            )
            await db.execute(
                "UPDATE extension_requests SET state='built' WHERE chat_key=$1 AND id=$2",
                chat.key,
                request_id,
            )
            return item_id

    async def get_revision(self, *, chat: ChatRef, revision_id: str) -> ExtensionRevision | None:
        row = await self.db.fetchrow(
            "SELECT * FROM extension_revisions WHERE chat_key=$1 AND id=$2", chat.key, revision_id
        )
        if not row:
            return None
        artifact = Artifact.from_json(
            row["artifact"] if isinstance(row["artifact"], str) else json.dumps(row["artifact"])
        )
        if artifact.digest != row["digest"]:
            return None
        return ExtensionRevision(
            row["id"],
            row["chat_key"],
            row["request_id"],
            row["author_key"],
            artifact,
            row["created_at"],
        )

    async def save_evidence(
        self,
        *,
        chat: ChatRef,
        revision_id: str,
        digest: str,
        passed: bool,
        image_id: str,
        policy_digest: str,
        harness_digest: str,
        case_count: int,
    ) -> None:
        async with self.db.transaction() as db:
            revision = await db.fetchrow(
                "SELECT digest FROM extension_revisions WHERE chat_key=$1 AND id=$2 FOR UPDATE",
                chat.key,
                revision_id,
            )
            if not revision or revision["digest"] != digest or case_count < 1:
                raise ValueError("revision unavailable")
            await db.execute(
                "INSERT INTO extension_evidence(chat_key,revision_id,digest,passed,image_id,policy_digest,harness_digest,case_count) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(chat_key,revision_id) DO UPDATE SET "
                "digest=EXCLUDED.digest,passed=EXCLUDED.passed,image_id=EXCLUDED.image_id,"
                "policy_digest=EXCLUDED.policy_digest,harness_digest=EXCLUDED.harness_digest,"
                "case_count=EXCLUDED.case_count,created_at=now()",
                chat.key,
                revision_id,
                digest,
                passed,
                image_id,
                policy_digest,
                harness_digest,
                case_count,
            )

    async def get_evidence(self, *, chat: ChatRef, revision_id: str) -> ExtensionEvidence | None:
        row = await self.db.fetchrow(
            "SELECT * FROM extension_evidence WHERE chat_key=$1 AND revision_id=$2",
            chat.key,
            revision_id,
        )
        return (
            ExtensionEvidence(
                row["revision_id"],
                row["digest"],
                row["passed"],
                row["image_id"],
                row["policy_digest"],
                row["harness_digest"],
                row["case_count"],
                row["created_at"],
            )
            if row
            else None
        )

    async def activate(
        self, *, chat: ChatRef, revision_id: str, reviewer: UserRef, digest: str
    ) -> bool:
        async with self.db.transaction() as db:
            revision = await db.fetchrow(
                "SELECT digest FROM extension_revisions WHERE chat_key=$1 AND id=$2 FOR UPDATE",
                chat.key,
                revision_id,
            )
            evidence = await db.fetchrow(
                "SELECT digest,passed,case_count FROM extension_evidence WHERE chat_key=$1 AND revision_id=$2 FOR UPDATE",
                chat.key,
                revision_id,
            )
            if (
                not revision
                or not evidence
                or revision["digest"] != digest
                or evidence["digest"] != digest
                or not evidence["passed"]
                or evidence["case_count"] < 1
            ):
                return False
            await db.execute(
                "INSERT INTO extension_activations(chat_key,revision_id,digest,reviewer_key,epoch,status,failures) "
                "VALUES($1,$2,$3,$4,1,'active',0) ON CONFLICT(chat_key,revision_id) DO UPDATE SET "
                "digest=EXCLUDED.digest,reviewer_key=EXCLUDED.reviewer_key,epoch=extension_activations.epoch+1,"
                "status='active',failures=0,updated_at=now()",
                chat.key,
                revision_id,
                digest,
                reviewer.key,
            )
            return True

    async def get_activation(
        self, *, chat: ChatRef, revision_id: str
    ) -> ExtensionActivation | None:
        row = await self.db.fetchrow(
            "SELECT * FROM extension_activations WHERE chat_key=$1 AND revision_id=$2 AND status='active'",
            chat.key,
            revision_id,
        )
        return _activation(row) if row else None

    async def revoke(self, *, chat: ChatRef, revision_id: str, reviewer: UserRef) -> bool:
        result = await self.db.execute(
            "UPDATE extension_activations SET status='revoked',reviewer_key=$3,epoch=epoch+1,updated_at=now() "
            "WHERE chat_key=$1 AND revision_id=$2 AND status<>'revoked'",
            chat.key,
            revision_id,
            reviewer.key,
        )
        return result.endswith("1")

    async def reserve_run(
        self, *, chat: ChatRef, revision_id: str, actor: UserRef, run_id: str
    ) -> tuple[ExtensionRevision, int] | None:
        async with self.db.transaction() as db:
            activation = await db.fetchrow(
                "SELECT * FROM extension_activations WHERE chat_key=$1 AND revision_id=$2 AND status='active' FOR UPDATE",
                chat.key,
                revision_id,
            )
            if activation is None:
                return None
            revision = await db.fetchrow(
                "SELECT * FROM extension_revisions WHERE chat_key=$1 AND id=$2",
                chat.key,
                revision_id,
            )
            if revision is None:
                return None
            quota = await db.fetchval(
                "INSERT INTO extension_quota(chat_key,utc_day,runs) "
                "VALUES($1,(now() AT TIME ZONE 'UTC')::date,1) "
                "ON CONFLICT(chat_key,utc_day) DO UPDATE "
                "SET runs=extension_quota.runs+1 WHERE extension_quota.runs<100 "
                "RETURNING runs",
                chat.key,
            )
            if quota is None:
                return None
            await db.execute(
                "INSERT INTO extension_runs(chat_key,run_id,revision_id,digest,epoch,actor_key,status) "
                "VALUES($1,$2,$3,$4,$5,$6,'running')",
                chat.key,
                run_id,
                revision_id,
                activation["digest"],
                activation["epoch"],
                actor.key,
            )
            artifact = Artifact.from_json(
                revision["artifact"]
                if isinstance(revision["artifact"], str)
                else json.dumps(revision["artifact"])
            )
            return ExtensionRevision(
                revision_id,
                chat.key,
                revision["request_id"],
                revision["author_key"],
                artifact,
                revision["created_at"],
            ), activation["epoch"]

    async def record_run(
        self,
        *,
        chat: ChatRef,
        revision_id: str,
        run_id: str,
        digest: str,
        epoch: int,
        actor: UserRef,
        result: RunResult,
    ) -> bool:
        async with self.db.transaction() as db:
            activation = await db.fetchrow(
                "SELECT * FROM extension_activations WHERE chat_key=$1 AND revision_id=$2 FOR UPDATE",
                chat.key,
                revision_id,
            )
            run = await db.fetchrow(
                "SELECT * FROM extension_runs WHERE chat_key=$1 AND run_id=$2 FOR UPDATE",
                chat.key,
                run_id,
            )
            if (
                not run
                or run["actor_key"] != actor.key
                or run["revision_id"] != revision_id
                or run["epoch"] != epoch
                or run["digest"] != digest
            ):
                return False
            await db.execute(
                "UPDATE extension_runs SET status=$3,duration_ms=$4,completed_at=now() "
                "WHERE chat_key=$1 AND run_id=$2",
                chat.key,
                run_id,
                result.status,
                result.duration_ms,
            )
            if (
                not activation
                or activation["status"] != "active"
                or activation["epoch"] != epoch
                or activation["digest"] != digest
            ):
                return False
            failures = 0 if result.status == "ok" else activation["failures"] + 1
            status = "suspended" if failures >= 3 else "active"
            await db.execute(
                "UPDATE extension_activations SET failures=$3,status=$4,updated_at=now() "
                "WHERE chat_key=$1 AND revision_id=$2",
                chat.key,
                revision_id,
                failures,
                status,
            )
            return True

    async def list_active(self, *, chat: ChatRef) -> list[ExtensionRevision]:
        rows = await self.db.fetch(
            "SELECT r.* FROM extension_revisions r JOIN extension_activations a "
            "ON a.chat_key=r.chat_key AND a.revision_id=r.id "
            "WHERE r.chat_key=$1 AND a.status='active' ORDER BY r.created_at",
            chat.key,
        )
        result = []
        for row in rows:
            item = await self.get_revision(chat=chat, revision_id=row["id"])
            if item:
                result.append(item)
        return result

    async def status(
        self,
        *,
        chat: ChatRef,
        revision_id: str | None = None,
        viewer: UserRef | None = None,
        include_drafts: bool = True,
    ) -> list[dict[str, object]]:
        rows = await self.db.fetch(
            "SELECT q.id AS request_id,q.author_key AS requester_key,q.state AS build_state,"
            "q.created_at AS requested_at,r.id AS revision_id,a.status AS activation_status,"
            "a.failures,a.epoch,a.updated_at,e.passed,e.case_count,"
            "COALESCE(rs.invocations,0)::int AS invocations,"
            "COALESCE(rs.successful_runs,0)::int AS successful_runs,"
            "COALESCE(rs.failed_runs,0)::int AS failed_runs,"
            "COALESCE(rs.running_runs,0)::int AS running_runs,"
            "COALESCE(quota.builds,0)::int AS builds_today,"
            "COALESCE(quota.runs,0)::int AS runs_today "
            "FROM extension_requests q "
            "LEFT JOIN extension_revisions r ON r.chat_key=q.chat_key AND r.request_id=q.id "
            "LEFT JOIN extension_activations a ON a.chat_key=r.chat_key AND a.revision_id=r.id "
            "LEFT JOIN extension_evidence e ON e.chat_key=r.chat_key AND e.revision_id=r.id "
            "LEFT JOIN LATERAL (SELECT count(*) AS invocations,"
            "count(*) FILTER (WHERE status='ok') AS successful_runs,"
            "count(*) FILTER (WHERE status NOT IN ('ok','running')) AS failed_runs,"
            "count(*) FILTER (WHERE status='running') AS running_runs "
            "FROM extension_runs WHERE chat_key=q.chat_key AND revision_id=r.id) rs ON true "
            "LEFT JOIN extension_quota quota ON quota.chat_key=q.chat_key "
            "AND quota.utc_day=(now() AT TIME ZONE 'UTC')::date "
            "WHERE q.chat_key=$1 AND ($2::text IS NULL OR r.id=$2 OR q.id=$2) "
            "ORDER BY q.created_at",
            chat.key,
            revision_id,
        )
        result = []
        for row in rows:
            if (
                row["activation_status"] is None
                and not include_drafts
                and (viewer is None or row["requester_key"] != viewer.key)
            ):
                continue
            item = {
                "request_id": row["request_id"],
                "revision_id": row["revision_id"],
                "requester_key": row["requester_key"],
                "status": row["activation_status"] or row["build_state"],
                "build_state": row["build_state"],
                "failures": row["failures"] or 0,
                "epoch": row["epoch"] or 0,
                "updated_at": row["updated_at"] or row["requested_at"],
                "test_status": (
                    "passed"
                    if row["passed"] is True
                    else "failed"
                    if row["passed"] is False
                    else "missing"
                ),
                "test_case_count": row["case_count"] or 0,
                "invocations": row["invocations"],
                "successful_runs": row["successful_runs"],
                "failed_runs": row["failed_runs"],
                "running_runs": row["running_runs"],
                "builds_today": row["builds_today"],
                "build_limit": 5,
                "runs_today": row["runs_today"],
                "run_limit": 100,
            }
            result.append(item)
        return result

    async def purge_user(self, *, chat: ChatRef, user: UserRef) -> None:
        async with self.db.transaction() as db:
            request_rows = await db.fetch(
                "SELECT id FROM extension_requests WHERE chat_key=$1 AND author_key=$2 FOR UPDATE",
                chat.key,
                user.key,
            )
            request_ids = [row["id"] for row in request_rows]
            revision_rows = await db.fetch(
                "SELECT id FROM extension_revisions WHERE chat_key=$1 AND "
                "(author_key=$2 OR request_id=ANY($3::text[])) FOR UPDATE",
                chat.key,
                user.key,
                request_ids,
            )
            revision_ids = {row["id"] for row in revision_rows}
            reviewer_rows = await db.fetch(
                "SELECT revision_id FROM extension_activations "
                "WHERE chat_key=$1 AND reviewer_key=$2 FOR UPDATE",
                chat.key,
                user.key,
            )
            revision_ids.update(row["revision_id"] for row in reviewer_rows)
            if revision_ids:
                await db.execute(
                    "UPDATE extension_activations SET status='revoked',epoch=epoch+1,updated_at=now() "
                    "WHERE chat_key=$1 AND revision_id=ANY($2::text[])",
                    chat.key,
                    list(revision_ids),
                )
            await db.execute(
                "DELETE FROM extension_requests WHERE chat_key=$1 AND author_key=$2",
                chat.key,
                user.key,
            )
            if revision_ids:
                await db.execute(
                    "DELETE FROM extension_revisions WHERE chat_key=$1 AND id=ANY($2::text[])",
                    chat.key,
                    list(revision_ids),
                )
            await db.execute(
                "DELETE FROM extension_runs WHERE chat_key=$1 AND actor_key=$2", chat.key, user.key
            )

    async def purge_chat(self, *, chat: ChatRef) -> None:
        async with self.db.transaction() as db:
            await db.execute("DELETE FROM extension_requests WHERE chat_key=$1", chat.key)
            await db.execute("DELETE FROM extension_revisions WHERE chat_key=$1", chat.key)
            await db.execute("DELETE FROM extension_quota WHERE chat_key=$1", chat.key)

    async def purge_older_than(self, cutoff: datetime) -> None:
        async with self.db.transaction() as db:
            await db.execute(
                "DELETE FROM extension_requests q WHERE created_at<$1 AND state<>'building' "
                "AND NOT EXISTS (SELECT 1 FROM extension_revisions r "
                "JOIN extension_activations a ON a.chat_key=r.chat_key AND a.revision_id=r.id "
                "WHERE r.chat_key=q.chat_key AND r.request_id=q.id "
                "AND a.status IN ('active','suspended'))",
                cutoff,
            )
            await db.execute(
                "DELETE FROM extension_revisions r WHERE created_at<$1 AND NOT EXISTS "
                "(SELECT 1 FROM extension_activations a WHERE a.chat_key=r.chat_key "
                "AND a.revision_id=r.id AND a.status IN ('active','suspended'))",
                cutoff,
            )
            await db.execute(
                "DELETE FROM extension_runs WHERE COALESCE(completed_at,created_at)<$1", cutoff
            )


def _cases(value: Any) -> tuple[ExampleCase, ...]:
    if isinstance(value, str):
        value = json.loads(value)
    return tuple(ExampleCase(item["arguments"], item["expected"]) for item in value)


def _activation(row: Any) -> ExtensionActivation:
    return ExtensionActivation(
        row["revision_id"],
        row["digest"],
        row["reviewer_key"],
        row["epoch"],
        row["status"],
        row["failures"],
        row["updated_at"],
    )
