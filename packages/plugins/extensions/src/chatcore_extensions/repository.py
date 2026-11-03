from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from chatcore.events import ChatRef, UserRef

from chatcore_extensions.models import Artifact, ExampleCase, RunResult


@dataclass(frozen=True)
class ExtensionRequest:
    id: str
    chat_key: str
    author_key: str
    description: str
    examples: tuple[ExampleCase, ...]
    state: str
    created_at: datetime


@dataclass(frozen=True)
class ExtensionRevision:
    id: str
    chat_key: str
    request_id: str
    author_key: str
    artifact: Artifact
    created_at: datetime


@dataclass(frozen=True)
class ExtensionEvidence:
    revision_id: str
    digest: str
    passed: bool
    image_id: str
    policy_digest: str
    harness_digest: str
    case_count: int
    created_at: datetime


@dataclass(frozen=True)
class ExtensionActivation:
    revision_id: str
    digest: str
    reviewer_key: str
    epoch: int
    status: str
    failures: int
    updated_at: datetime


class ExtensionRepo(Protocol):
    async def create_request(
        self, *, chat: ChatRef, actor: UserRef, description: str, examples: tuple[ExampleCase, ...]
    ) -> str: ...
    async def get_request(self, *, chat: ChatRef, request_id: str) -> ExtensionRequest | None: ...
    async def claim_build(self, *, chat: ChatRef, request_id: str, actor: UserRef) -> bool: ...
    async def save_revision(
        self, *, chat: ChatRef, request_id: str, actor: UserRef, artifact: Artifact
    ) -> str: ...
    async def get_revision(
        self, *, chat: ChatRef, revision_id: str
    ) -> ExtensionRevision | None: ...
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
    ) -> None: ...
    async def get_evidence(
        self, *, chat: ChatRef, revision_id: str
    ) -> ExtensionEvidence | None: ...
    async def activate(
        self, *, chat: ChatRef, revision_id: str, reviewer: UserRef, digest: str
    ) -> bool: ...
    async def get_activation(
        self, *, chat: ChatRef, revision_id: str
    ) -> ExtensionActivation | None: ...
    async def revoke(self, *, chat: ChatRef, revision_id: str, reviewer: UserRef) -> bool: ...
    async def reserve_run(
        self, *, chat: ChatRef, revision_id: str, actor: UserRef, run_id: str
    ) -> tuple[ExtensionRevision, int] | None: ...
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
    ) -> bool: ...
    async def list_active(self, *, chat: ChatRef) -> list[ExtensionRevision]: ...
    async def status(
        self,
        *,
        chat: ChatRef,
        revision_id: str | None = None,
        viewer: UserRef | None = None,
        include_drafts: bool = True,
    ) -> list[dict[str, object]]: ...
    async def purge_user(self, *, chat: ChatRef, user: UserRef) -> None: ...
    async def purge_chat(self, *, chat: ChatRef) -> None: ...
    async def purge_older_than(self, cutoff: datetime) -> None: ...


class MemoryExtensionRepo:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._requests: dict[tuple[str, str], ExtensionRequest] = {}
        self._revisions: dict[tuple[str, str], ExtensionRevision] = {}
        self._evidence: dict[tuple[str, str], ExtensionEvidence] = {}
        self._activations: dict[tuple[str, str], ExtensionActivation] = {}
        self._runs: dict[tuple[str, str], dict[str, object]] = {}
        self._quotas: dict[tuple[str, str], dict[str, int]] = {}

    async def create_request(
        self, *, chat: ChatRef, actor: UserRef, description: str, examples: tuple[ExampleCase, ...]
    ) -> str:
        async with self._lock:
            pending = sum(
                r.chat_key == chat.key and r.state == "requested" for r in self._requests.values()
            )
            if pending >= 100:
                raise ValueError("scope proposal limit reached")
            item_id = secrets.token_hex(12)
            self._requests[(chat.key, item_id)] = ExtensionRequest(
                item_id, chat.key, actor.key, description, examples, "requested", datetime.now(UTC)
            )
            return item_id

    async def get_request(self, *, chat: ChatRef, request_id: str) -> ExtensionRequest | None:
        return self._requests.get((chat.key, request_id))

    async def claim_build(self, *, chat: ChatRef, request_id: str, actor: UserRef) -> bool:
        async with self._lock:
            key = (chat.key, request_id)
            request = self._requests.get(key)
            if request is None or request.state != "requested":
                return False
            quota = self._quota(chat)
            if quota["builds"] >= 5:
                return False
            quota["builds"] += 1
            self._requests[key] = ExtensionRequest(
                request.id,
                request.chat_key,
                request.author_key,
                request.description,
                request.examples,
                "building",
                request.created_at,
            )
            return True

    async def fail_build(self, *, chat: ChatRef, request_id: str) -> None:
        async with self._lock:
            key = (chat.key, request_id)
            request = self._requests.get(key)
            if request and request.state == "building":
                self._requests[key] = ExtensionRequest(
                    request.id,
                    request.chat_key,
                    request.author_key,
                    request.description,
                    request.examples,
                    "failed",
                    request.created_at,
                )

    async def save_revision(
        self, *, chat: ChatRef, request_id: str, actor: UserRef, artifact: Artifact
    ) -> str:
        async with self._lock:
            key = (chat.key, request_id)
            request = self._requests.get(key)
            if request is None or request.state != "building":
                raise ValueError("request unavailable")
            revision_id = secrets.token_hex(12)
            self._revisions[(chat.key, revision_id)] = ExtensionRevision(
                revision_id, chat.key, request_id, actor.key, artifact, datetime.now(UTC)
            )
            self._requests[key] = ExtensionRequest(
                request.id,
                request.chat_key,
                request.author_key,
                request.description,
                request.examples,
                "built",
                request.created_at,
            )
            return revision_id

    async def get_revision(self, *, chat: ChatRef, revision_id: str) -> ExtensionRevision | None:
        return self._revisions.get((chat.key, revision_id))

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
        async with self._lock:
            revision = self._revisions.get((chat.key, revision_id))
            if revision is None or revision.artifact.digest != digest:
                raise ValueError("revision unavailable")
            self._evidence[(chat.key, revision_id)] = ExtensionEvidence(
                revision_id,
                digest,
                passed,
                image_id,
                policy_digest,
                harness_digest,
                case_count,
                datetime.now(UTC),
            )

    async def get_evidence(self, *, chat: ChatRef, revision_id: str) -> ExtensionEvidence | None:
        return self._evidence.get((chat.key, revision_id))

    async def activate(
        self, *, chat: ChatRef, revision_id: str, reviewer: UserRef, digest: str
    ) -> bool:
        async with self._lock:
            key = (chat.key, revision_id)
            revision = self._revisions.get(key)
            evidence = self._evidence.get(key)
            if revision is None or revision.artifact.digest != digest or evidence is None:
                return False
            if evidence.digest != digest or not evidence.passed or evidence.case_count < 1:
                return False
            previous = self._activations.get(key)
            epoch = (previous.epoch + 1) if previous else 1
            self._activations[key] = ExtensionActivation(
                revision_id, digest, reviewer.key, epoch, "active", 0, datetime.now(UTC)
            )
            return True

    async def get_activation(
        self, *, chat: ChatRef, revision_id: str
    ) -> ExtensionActivation | None:
        activation = self._activations.get((chat.key, revision_id))
        return activation if activation and activation.status == "active" else None

    async def revoke(self, *, chat: ChatRef, revision_id: str, reviewer: UserRef) -> bool:
        async with self._lock:
            key = (chat.key, revision_id)
            current = self._activations.get(key)
            if current is None:
                return False
            self._activations[key] = ExtensionActivation(
                current.revision_id,
                current.digest,
                reviewer.key,
                current.epoch + 1,
                "revoked",
                current.failures,
                datetime.now(UTC),
            )
            return True

    async def reserve_run(
        self, *, chat: ChatRef, revision_id: str, actor: UserRef, run_id: str
    ) -> tuple[ExtensionRevision, int] | None:
        async with self._lock:
            key = (chat.key, revision_id)
            activation = self._activations.get(key)
            revision = self._revisions.get(key)
            if not revision or not activation or activation.status != "active":
                return None
            quota = self._quota(chat)
            if quota["runs"] >= 100 or (chat.key, run_id) in self._runs:
                return None
            quota["runs"] += 1
            self._runs[(chat.key, run_id)] = {
                "revision_id": revision_id,
                "digest": activation.digest,
                "epoch": activation.epoch,
                "actor": actor.key,
                "status": "running",
                "duration_ms": 0,
                "created_at": datetime.now(UTC),
            }
            return revision, activation.epoch

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
        async with self._lock:
            run_key = (chat.key, run_id)
            run = self._runs.get(run_key)
            activation_key = (chat.key, revision_id)
            activation = self._activations.get(activation_key)
            if (
                not run
                or run["revision_id"] != revision_id
                or run["epoch"] != epoch
                or run["digest"] != digest
                or run["actor"] != actor.key
            ):
                return False
            completed_at = datetime.now(UTC)
            self._runs[run_key] = {
                **run,
                "status": result.status,
                "duration_ms": result.duration_ms,
                "completed_at": completed_at,
            }
            if (
                not activation
                or activation.status != "active"
                or activation.epoch != epoch
                or activation.digest != digest
            ):
                return False
            failures = 0 if result.status == "ok" else activation.failures + 1
            status = "suspended" if failures >= 3 else "active"
            self._activations[activation_key] = ExtensionActivation(
                activation.revision_id,
                activation.digest,
                activation.reviewer_key,
                activation.epoch,
                status,
                failures,
                datetime.now(UTC),
            )
            return True

    async def list_active(self, *, chat: ChatRef) -> list[ExtensionRevision]:
        return [
            revision
            for revision in self._revisions.values()
            if revision.chat_key == chat.key
            and (activation := self._activations.get((chat.key, revision.id)))
            and activation.status == "active"
        ]

    async def status(
        self,
        *,
        chat: ChatRef,
        revision_id: str | None = None,
        viewer: UserRef | None = None,
        include_drafts: bool = True,
    ) -> list[dict[str, object]]:
        async with self._lock:
            day = datetime.now(UTC).date().isoformat()
            quota = self._quotas.get((chat.key, day), {"builds": 0, "runs": 0})
            rows: list[dict[str, object]] = []
            for (scope, request_id), request in self._requests.items():
                if scope != chat.key or (
                    revision_id is not None
                    and revision_id
                    not in {
                        request_id,
                        *[
                            rid
                            for (r_scope, rid), rev in self._revisions.items()
                            if r_scope == scope and rev.request_id == request_id
                        ],
                    }
                ):
                    continue
                revisions = [
                    item
                    for (r_scope, _), item in self._revisions.items()
                    if r_scope == scope and item.request_id == request_id
                ]
                if not revisions:
                    if not include_drafts and (viewer is None or request.author_key != viewer.key):
                        continue
                    rows.append(
                        self._status_row(
                            request, None, None, quota, [], requester_key=request.author_key
                        )
                    )
                    continue
                for revision in revisions:
                    activation = self._activations.get((scope, revision.id))
                    if (
                        activation is None
                        and not include_drafts
                        and (viewer is None or request.author_key != viewer.key)
                    ):
                        continue
                    evidence = self._evidence.get((scope, revision.id))
                    runs = [
                        item
                        for (run_scope, _), item in self._runs.items()
                        if run_scope == scope and item.get("revision_id") == revision.id
                    ]
                    rows.append(
                        self._status_row(
                            request,
                            revision.id,
                            activation,
                            quota,
                            runs,
                            evidence=evidence,
                            requester_key=request.author_key,
                        )
                    )
            return rows

    @staticmethod
    def _status_row(
        request: ExtensionRequest,
        revision_id: str | None,
        activation: ExtensionActivation | None,
        quota: dict[str, int],
        runs: list[dict[str, object]],
        *,
        evidence: ExtensionEvidence | None = None,
        requester_key: str,
    ) -> dict[str, object]:
        return {
            "request_id": request.id,
            "revision_id": revision_id,
            "requester_key": requester_key,
            "status": activation.status if activation else request.state,
            "build_state": request.state,
            "failures": activation.failures if activation else 0,
            "epoch": activation.epoch if activation else 0,
            "updated_at": activation.updated_at if activation else request.created_at,
            "test_status": "passed"
            if evidence and evidence.passed
            else "failed"
            if evidence
            else "missing",
            "test_case_count": evidence.case_count if evidence else 0,
            "invocations": len(runs),
            "successful_runs": sum(item.get("status") == "ok" for item in runs),
            "failed_runs": sum(item.get("status") not in {"ok", "running"} for item in runs),
            "running_runs": sum(item.get("status") == "running" for item in runs),
            "builds_today": quota["builds"],
            "build_limit": 5,
            "runs_today": quota["runs"],
            "run_limit": 100,
        }

    async def purge_user(self, *, chat: ChatRef, user: UserRef) -> None:
        async with self._lock:
            scope = chat.key
            requester_requests = {
                request_id
                for (sc, request_id), request in self._requests.items()
                if sc == scope and request.author_key == user.key
            }
            to_delete = {
                rid
                for (sc, rid), rev in self._revisions.items()
                if sc == scope
                and (rev.author_key == user.key or rev.request_id in requester_requests)
            }
            for key, activation in list(self._activations.items()):
                if key[0] == scope and activation.reviewer_key == user.key:
                    self._activations[key] = ExtensionActivation(
                        activation.revision_id,
                        activation.digest,
                        user.key,
                        activation.epoch + 1,
                        "revoked",
                        activation.failures,
                        datetime.now(UTC),
                    )
                    to_delete.add(key[1])
            for key, request in list(self._requests.items()):
                if key[0] == scope and request.author_key == user.key:
                    self._requests.pop(key)
            for key in [key for key in self._revisions if key[0] == scope and key[1] in to_delete]:
                self._revisions.pop(key, None)
                self._evidence.pop(key, None)
                self._activations.pop(key, None)
            for key, run in list(self._runs.items()):
                if key[0] == scope and (
                    run.get("actor") == user.key or run.get("revision_id") in to_delete
                ):
                    self._runs.pop(key)

    async def purge_chat(self, *, chat: ChatRef) -> None:
        async with self._lock:
            for mapping in (
                self._requests,
                self._revisions,
                self._evidence,
                self._activations,
                self._runs,
            ):
                for key in [key for key in mapping if key[0] == chat.key]:
                    mapping.pop(key, None)
            for key in [key for key in self._quotas if key[0] == chat.key]:
                self._quotas.pop(key, None)

    async def purge_older_than(self, cutoff: datetime) -> None:
        async with self._lock:
            retained = {
                key
                for key, item in self._activations.items()
                if item.status in {"active", "suspended"}
            }
            retained_requests = {
                (scope, revision.request_id)
                for (scope, revision_id), revision in self._revisions.items()
                if (scope, revision_id) in retained
            }
            for key, request in list(self._requests.items()):
                if request.created_at < cutoff and key not in retained_requests:
                    self._requests.pop(key, None)
            for key, revision in list(self._revisions.items()):
                if revision.created_at < cutoff and key not in retained:
                    self._revisions.pop(key, None)
                    self._evidence.pop(key, None)
            for key, run in list(self._runs.items()):
                if (run.get("completed_at") or run["created_at"]) < cutoff:
                    self._runs.pop(key, None)

    def _quota(self, chat: ChatRef) -> dict[str, int]:
        day = datetime.now(UTC).date().isoformat()
        return self._quotas.setdefault((chat.key, day), {"builds": 0, "runs": 0})
