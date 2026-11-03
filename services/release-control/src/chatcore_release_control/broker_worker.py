"""Durable protected dispatch and typed run reconciliation; never guesses latest runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx

from .broker import BrokerStore
from .ledger import ReleaseLedger, RunAttemptBinding
from .revocation import is_revoked

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_RETRYABLE = {"cancelled", "failure", "timed_out", "skipped", "action_required", "startup_failure"}
_STATUSES = {"queued", "in_progress", "completed", "waiting", "pending", "requested"}
_CONCLUSIONS = _RETRYABLE | {"success", "neutral", "stale"}


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


class WorkerError(ValueError):
    def __init__(self, code="worker_unavailable"):
        self.code = (
            code
            if code in {"worker_unavailable", "invalid_response", "reconciliation_required"}
            else "worker_unavailable"
        )
        super().__init__(self.code)


class BrokerWorkerStore:
    def __init__(self, pool):
        self.pool = pool
        self.broker = BrokerStore(pool)

    async def ready(self):
        return (
            await self.pool.fetchval(
                "SELECT to_regclass('release_control.broker_dispatch_intent') IS NOT NULL "
                "AND to_regclass('release_control.release_run_history') IS NOT NULL "
                "AND to_regclass('release_control.broker_release_revocation') IS NOT NULL"
            )
            is True
        )

    async def intent_state(self, approval_id: UUID, fence: int):
        return await self.pool.fetchval(
            "SELECT state FROM release_control.broker_dispatch_intent "
            "WHERE approval_id=$1 AND dispatch_fence=$2",
            approval_id,
            fence,
        )

    async def dispatch_id(self, approval_id: UUID, fence: int) -> UUID:
        value = await self.pool.fetchval(
            "SELECT dispatch_id FROM release_control.broker_dispatch_intent "
            "WHERE approval_id=$1 AND dispatch_fence=$2",
            approval_id,
            fence,
        )
        if not isinstance(value, UUID):
            raise WorkerError("reconciliation_required")
        return value

    async def begin_dispatch(self, approval_id, fence, now):
        context = await self.broker.get_release_context(approval_id, now=now)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                queue = await connection.fetchrow(
                    "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                    context.target,
                )
                card = await connection.fetchrow(
                    "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE", approval_id
                )
                state = await connection.fetchrow(
                    "SELECT * FROM release_control.broker_target_state WHERE target=$1 FOR UPDATE",
                    context.target,
                )
                if (
                    queue["active_approval_id"] != approval_id
                    or queue["active_fence"] != fence
                    or card["fence"] != fence
                ):
                    raise WorkerError("reconciliation_required")
                existing = await connection.fetchval(
                    "SELECT state FROM release_control.broker_dispatch_intent "
                    "WHERE approval_id=$1 AND dispatch_fence=$2",
                    approval_id,
                    fence,
                )
                if existing is not None:
                    return existing, context
                if await is_revoked(connection, approval_id):
                    await connection.execute(
                        "UPDATE release_control.release_card SET status='invalidated' WHERE id=$1",
                        approval_id,
                    )
                    await connection.execute(
                        "UPDATE release_control.decision_outbox SET state='canceled',"
                        "lease_owner=NULL,lease_until=NULL,dispatched_at=NULL "
                        "WHERE approval_id=$1",
                        approval_id,
                    )
                    await connection.execute(
                        "UPDATE release_control.target_queue SET active_approval_id=NULL,"
                        "active_fence=NULL,active_until=NULL WHERE target=$1",
                        context.target,
                    )
                    return "invalidated", context
                binding = _json(card["binding"])
                if (
                    card["status"] != "queued"
                    or card["decision"] != "approve"
                    or card["expires_at"] <= now
                    or state["reconciliation_required"]
                    or state["telegram_owner_user_id"] != card["owner_user_id"]
                    or state["policy_digest"] != binding["policy_digest"]
                    or _json(state["deployed_identity"]) != binding["deployed_identity"]
                    or context.repository != state["repository"]
                ):
                    if card["status"] != "queued" or card["run_id"] is not None:
                        raise WorkerError("reconciliation_required")
                    await connection.execute(
                        "UPDATE release_control.release_card SET status='invalidated' WHERE id=$1",
                        approval_id,
                    )
                    await connection.execute(
                        "UPDATE release_control.decision_outbox SET state='canceled',"
                        "lease_owner=NULL,lease_until=NULL,dispatched_at=NULL WHERE approval_id=$1",
                        approval_id,
                    )
                    await connection.execute(
                        "UPDATE release_control.target_queue SET active_approval_id=NULL,"
                        "active_fence=NULL,active_until=NULL WHERE target=$1",
                        context.target,
                    )
                    return "invalidated", context
                dispatch_id = uuid4()
                request = {
                    "dispatch_id": str(dispatch_id),
                    "approval_id": str(approval_id),
                    "pr_number": context.pr_number,
                    "head_sha": context.head_sha,
                }
                await connection.execute(
                    "INSERT INTO release_control.broker_dispatch_intent "
                    "(approval_id,dispatch_fence,request_digest,state,created_at,updated_at,"
                    "dispatch_id) "
                    "VALUES($1,$2,$3,'sending',$4,$4,$5)",
                    approval_id,
                    fence,
                    hashlib.sha256(_canonical(request)).digest(),
                    now,
                    dispatch_id,
                )
                # A signed run claim can arrive before the HTTP reply. It may
                # bind only after this durable intent; dispatch itself grants no admission.
                await connection.execute(
                    "UPDATE release_control.decision_outbox SET state='dispatched',"
                    "lease_owner=NULL,lease_until=NULL,dispatched_at=$2 WHERE approval_id=$1",
                    approval_id,
                    now,
                )
                await connection.execute(
                    "UPDATE release_control.target_queue SET active_until=NULL WHERE target=$1",
                    context.target,
                )
                return "new", context

    async def dispatch_result(self, approval_id, fence, *, accepted, now):
        await self.pool.execute(
            "UPDATE release_control.broker_dispatch_intent SET state=$3,updated_at=$4 "
            "WHERE approval_id=$1 AND dispatch_fence=$2 AND state='sending'",
            approval_id,
            fence,
            "accepted" if accepted else "uncertain",
            now,
        )

    async def requeue_before_admission(
        self,
        approval_id,
        *,
        fence,
        run: RunAttemptBinding,
        conclusion,
        current_base,
        current_head,
        pr_open,
        now,
    ):
        if conclusion not in _RETRYABLE or pr_open is not True:
            return False
        context = await self.broker.get_release_context(approval_id, now=now)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                queue = await connection.fetchrow(
                    "SELECT * FROM release_control.target_queue WHERE target=$1 FOR UPDATE",
                    context.target,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM release_control.release_card WHERE id=$1 FOR UPDATE", approval_id
                )
                state = await connection.fetchrow(
                    "SELECT * FROM release_control.broker_target_state WHERE target=$1 FOR UPDATE",
                    context.target,
                )
                binding = _json(row["binding"])
                admitted = await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM release_control.broker_merge_admission "
                    "WHERE approval_id=$1)",
                    approval_id,
                )
                if not admitted and await is_revoked(connection, approval_id):
                    has_lease = await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM release_control.broker_merge_lease "
                        "WHERE approval_id=$1 AND released_at IS NULL)",
                        approval_id,
                    )
                    if not has_lease:
                        await connection.execute(
                            "UPDATE release_control.release_card SET status='invalidated' "
                            "WHERE id=$1",
                            approval_id,
                        )
                        await connection.execute(
                            "UPDATE release_control.decision_outbox SET state='canceled',"
                            "lease_owner=NULL,lease_until=NULL,dispatched_at=NULL "
                            "WHERE approval_id=$1",
                            approval_id,
                        )
                        await connection.execute(
                            "UPDATE release_control.target_queue SET active_approval_id=NULL,"
                            "active_fence=NULL,active_until=NULL WHERE target=$1",
                            context.target,
                        )
                    return False
                if (
                    admitted
                    or row["status"] != "queued"
                    or row["decision"] != "approve"
                    or row["expires_at"] <= now
                    or queue["active_approval_id"] != approval_id
                    or queue["active_fence"] != fence
                    or row["fence"] != fence
                    or row["run_id"] is None
                    or ReleaseLedger._run_binding(row) != run
                    or current_base != binding["base_sha"]
                    or current_head != binding["head_sha"]
                    or state["reconciliation_required"]
                    or state["telegram_owner_user_id"] != row["owner_user_id"]
                    or state["policy_digest"] != binding["policy_digest"]
                    or _json(state["deployed_identity"]) != binding["deployed_identity"]
                ):
                    return False
                await connection.execute(
                    "INSERT INTO release_control.release_run_history "
                    "(approval_id,dispatch_fence,repository_id,run_id,run_attempt,workflow_ref,"
                    "workflow_sha,check_run_id,run_bound_at,conclusion,observed_at) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                    approval_id,
                    fence,
                    run.repository_id,
                    run.run_id,
                    run.attempt,
                    run.workflow_ref,
                    run.workflow_sha,
                    run.check_run_id,
                    row["run_bound_at"],
                    conclusion,
                    now,
                )
                await connection.execute(
                    "UPDATE release_control.release_card SET fence=$2,run_repository_id=NULL,"
                    "run_id=NULL,run_attempt=NULL,workflow_ref=NULL,workflow_sha=NULL,"
                    "check_run_id=NULL,run_bound_at=NULL WHERE id=$1",
                    approval_id,
                    queue["next_fence"],
                )
                await connection.execute(
                    "UPDATE release_control.broker_merge_lease SET released_at=$2 "
                    "WHERE approval_id=$1 AND released_at IS NULL",
                    approval_id,
                    now,
                )
                await connection.execute(
                    "UPDATE release_control.decision_outbox SET state='pending',"
                    "lease_owner=NULL,lease_until=NULL,dispatched_at=NULL WHERE approval_id=$1",
                    approval_id,
                )
                await connection.execute(
                    "UPDATE release_control.target_queue SET active_approval_id=NULL,"
                    "active_fence=NULL,active_until=NULL WHERE target=$1",
                    context.target,
                )
                await connection.execute(
                    "UPDATE release_control.broker_dispatch_intent SET state='retired',"
                    "updated_at=$3 WHERE approval_id=$1 AND dispatch_fence=$2 AND state<>'retired'",
                    approval_id,
                    fence,
                    now,
                )
                return True

    async def observe_run(self, approval_id, fence, run, observation, now):
        if (
            await self.pool.fetchval(
                "SELECT count(*) FROM release_control.broker_run_observation WHERE approval_id=$1",
                approval_id,
            )
            >= 4096
        ):
            raise WorkerError("reconciliation_required")
        await self.pool.execute(
            "INSERT INTO release_control.broker_run_observation "
            "(approval_id,dispatch_fence,run_id,run_attempt,status,conclusion,observed_at) "
            "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING",
            approval_id,
            fence,
            run.run_id,
            run.attempt,
            observation["status"],
            observation["conclusion"],
            now,
        )


class BrokerDispatchWorker:
    def __init__(self, *, store: BrokerWorkerStore, ledger: ReleaseLedger, api, reporter):
        self.store, self.ledger, self.api, self.reporter = store, ledger, api, reporter

    async def dispatch(self, approval_id, *, fence, now=None):
        now = now or datetime.now(UTC)
        state, context = await self.store.begin_dispatch(approval_id, fence, now)
        if state == "invalidated":
            return state
        if state != "new":
            return (
                "dispatch_uncertain" if state in {"sending", "uncertain"} else "awaiting_run_claim"
            )
        try:
            await self.api.dispatch(context, await self.store.dispatch_id(approval_id, fence))
        except Exception:
            await self.store.dispatch_result(approval_id, fence, accepted=False, now=now)
            return "dispatch_uncertain"
        await self.store.dispatch_result(approval_id, fence, accepted=True, now=now)
        return "awaiting_run_claim"

    async def poll(self, approval_id, *, now=None):
        now = now or datetime.now(UTC)
        card = await self.ledger.get_card(approval_id)
        if card.run_attempt is None:
            return "awaiting_run_claim"
        run = card.run_attempt
        observation = await self.api.run_status(
            run, await self.store.dispatch_id(approval_id, card.fence)
        )
        await self.store.observe_run(approval_id, card.fence, run, observation, now)
        if observation["status"] == "completed" and observation["conclusion"] in _RETRYABLE:
            if card.status.value == "queued":
                pull = await self.api.pull_state(card.binding["pr_number"])
                if await self.store.requeue_before_admission(
                    approval_id,
                    fence=card.fence,
                    run=run,
                    conclusion=observation["conclusion"],
                    current_base=pull["base"],
                    current_head=pull["head"],
                    pr_open=pull["open"],
                    now=now,
                ):
                    return "requeued"
            return "reconciliation_required"
        return observation["status"]

    async def tick(self, target: str, worker_id: str, *, now=None):
        now = now or datetime.now(UTC)
        await self.ledger.expire_pending(now=now)
        queue = await self.store.pool.fetchrow(
            "SELECT * FROM release_control.target_queue WHERE target=$1",
            target,
        )
        active = queue["active_approval_id"] if queue else None
        if active is not None:
            state = await self.store.intent_state(active, queue["active_fence"])
            if state is not None:
                card = await self.ledger.get_card(active)
                outcome = (
                    await self.poll(active, now=now)
                    if card.run_attempt
                    else (
                        "dispatch_uncertain"
                        if state in {"sending", "uncertain"}
                        else "awaiting_run_claim"
                    )
                )
            else:
                claim = await self.ledger.claim_next_dispatch(target, worker_id, now=now)
                outcome = (
                    await self.dispatch(claim.approval_id, fence=claim.fence, now=now)
                    if claim
                    else "waiting"
                )
        else:
            claim = await self.ledger.claim_next_dispatch(target, worker_id, now=now)
            outcome = (
                await self.dispatch(claim.approval_id, fence=claim.fence, now=now)
                if claim
                else "idle"
            )
        if self.reporter is not None:
            rows = await self.store.pool.fetch(
                "WITH reports AS (SELECT c.id,c.sequence,c.status || ':' || "
                "COALESCE(c.fence,0)::text || ':' || COALESCE(i.state,'none') || ':' || "
                "COALESCE(r.status,'none') || ':' || COALESCE(r.conclusion,'none') AS report_key "
                "FROM release_control.release_card c "
                "JOIN release_control.broker_target_state s ON s.target=c.target "
                "LEFT JOIN release_control.broker_dispatch_intent i "
                "ON i.approval_id=c.id AND i.dispatch_fence=c.fence "
                "LEFT JOIN LATERAL (SELECT status,conclusion "
                "FROM release_control.broker_run_observation r "
                "WHERE r.approval_id=c.id AND r.dispatch_fence=c.fence "
                "ORDER BY r.observed_at DESC LIMIT 1) r ON true "
                "WHERE c.target=$1 AND c.status<>'awaiting_approval' "
                "AND c.owner_user_id=s.telegram_owner_user_id) "
                "SELECT c.id,c.report_key FROM reports c "
                "LEFT JOIN release_control.broker_report_delivery d "
                "ON d.approval_id=c.id AND d.release_status=c.report_key "
                "WHERE COALESCE(d.delivered,false)=false "
                "ORDER BY c.sequence LIMIT 32",
                target,
            )
            for row in rows:
                request = uuid5(NAMESPACE_URL, f"chatcore-release:{row['id']}:{row['report_key']}")
                await self.store.pool.execute(
                    "INSERT INTO release_control.broker_report_delivery "
                    "(approval_id,release_status,request_id) VALUES($1,$2,$3) "
                    "ON CONFLICT DO NOTHING",
                    row["id"],
                    row["report_key"],
                    request,
                )
                await self.reporter.report(request, row["id"])
                await self.store.pool.execute(
                    "UPDATE release_control.broker_report_delivery SET delivered=true "
                    "WHERE approval_id=$1 AND release_status=$2",
                    row["id"],
                    row["report_key"],
                )
        return outcome


class BrokerGitHubClient:
    def __init__(self, *, repository, repository_id, owner_id, token_provider, transport=None):
        if (
            not isinstance(repository, str)
            or not _REPOSITORY.fullmatch(repository)
            or any(type(value) is not int or value <= 0 for value in (repository_id, owner_id))
            or not callable(token_provider)
        ):
            raise WorkerError()
        self.repository, self.repository_id, self.owner_id = repository, repository_id, owner_id
        self._token = token_provider
        self._http = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(8, connect=4),
        )

    async def _request(self, method, path="", *, body=None, expected=200):
        if (
            method not in {"GET", "POST"}
            or not re.fullmatch(r"[A-Za-z0-9_./-]*", path)
            or ".." in path.split("/")
        ):
            raise WorkerError()
        token = await asyncio.to_thread(self._token)
        if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_.,-]{1,4096}", token) is None:
            raise WorkerError()
        try:
            async with asyncio.timeout(20):
                async with self._http.stream(
                    method,
                    f"https://api.github.com/repos/{self.repository}"
                    + ("/" + path if path else ""),
                    json=body,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                ) as response:
                    if response.status_code != expected:
                        raise WorkerError()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(8192):
                        if len(raw) + len(chunk) > 1024 * 1024:
                            raise WorkerError()
                        raw.extend(chunk)
                if expected == 204:
                    if raw:
                        raise WorkerError()
                    return None
                result = json.loads(raw, object_pairs_hook=_unique)
                if not isinstance(result, dict):
                    raise WorkerError()
                return result
        except Exception:
            raise WorkerError() from None

    async def _verify_repository(self):
        result = await self._request("GET")
        if (
            type(result.get("id")) is not int
            or result["id"] != self.repository_id
            or result.get("full_name") != self.repository
            or result.get("private") is not True
            or not isinstance(result.get("owner"), dict)
            or type(result["owner"].get("id")) is not int
            or result["owner"]["id"] != self.owner_id
        ):
            raise WorkerError("invalid_response")

    async def dispatch(self, context, dispatch_id: UUID):
        await self._verify_repository()
        if (
            not isinstance(dispatch_id, UUID)
            or not isinstance(context.head_sha, str)
            or not _SHA.fullmatch(context.head_sha)
            or type(context.pr_number) is not int
            or context.pr_number <= 0
        ):
            raise WorkerError()
        approval = str(UUID(str(context.approval_id)))
        await self._request(
            "POST",
            "actions/workflows/release.yml/dispatches",
            expected=204,
            body={
                "ref": "main",
                "inputs": {
                    "dispatch_id": str(dispatch_id),
                    "approval_id": approval,
                    "pr_number": str(context.pr_number),
                    "head_sha": context.head_sha,
                },
            },
        )

    async def run_status(self, run: RunAttemptBinding, dispatch_id: UUID):
        await self._verify_repository()
        result = await self._request("GET", f"actions/runs/{run.run_id}/attempts/{run.attempt}")
        if (
            type(result.get("id")) is not int
            or result["id"] != run.run_id
            or type(result.get("run_attempt")) is not int
            or result["run_attempt"] != run.attempt
            or result.get("head_sha") != run.workflow_sha
            or result.get("path") != ".github/workflows/release.yml"
            or result.get("display_title") != f"release:{dispatch_id}"
            or result.get("event") != "workflow_dispatch"
            or result.get("head_branch") != "main"
            or not isinstance(result.get("repository"), dict)
            or result["repository"].get("id") != self.repository_id
            or result.get("status") not in _STATUSES
            or result.get("status") == "completed"
            and result.get("conclusion") not in _CONCLUSIONS
            or result.get("status") != "completed"
            and result.get("conclusion") is not None
        ):
            raise WorkerError("invalid_response")
        return {"status": result["status"], "conclusion": result.get("conclusion")}

    async def pull_state(self, number):
        if type(number) is not int or number <= 0:
            raise WorkerError()
        await self._verify_repository()
        result = await self._request("GET", f"pulls/{number}")
        try:
            if (
                result["number"] != number
                or result["base"]["repo"]["id"] != self.repository_id
                or result["head"]["repo"]["id"] != self.repository_id
                or result["base"]["ref"] != "main"
                or not _SHA.fullmatch(result["base"]["sha"])
                or not _SHA.fullmatch(result["head"]["sha"])
            ):
                raise ValueError
            return {
                "base": result["base"]["sha"],
                "head": result["head"]["sha"],
                "open": result["state"] == "open" and result["merged"] is False,
            }
        except Exception:
            raise WorkerError("invalid_response") from None

    async def close(self):
        await self._http.aclose()
