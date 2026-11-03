from __future__ import annotations

import asyncio

import pytest
from chatcore.events import ChatRef, UserRef
from chatcore_extensions.models import Artifact, ExampleCase, RunResult
from chatcore_extensions.repository import MemoryExtensionRepo


@pytest.mark.asyncio
async def test_repository_scopes_known_foreign_ids_and_purges_only_target_scope() -> None:
    repo = MemoryExtensionRepo()
    group_a, group_b, dm = ChatRef("tg", "-1001"), ChatRef("tg", "-1002"), ChatRef("tg", "7")
    user = UserRef("tg", "7")
    artifact = Artifact("identity", "def run(arguments): return arguments", (ExampleCase({}, {}),))
    request = await repo.create_request(
        chat=group_a, actor=user, description="x", examples=artifact.cases
    )
    assert await repo.get_request(chat=group_b, request_id=request) is None
    assert await repo.get_request(chat=dm, request_id=request) is None
    assert await repo.get_request(chat=group_a, request_id=request) is not None
    await repo.purge_chat(chat=group_b)
    assert await repo.get_request(chat=group_a, request_id=request) is not None


@pytest.mark.asyncio
async def test_build_quota_claim_and_activation_evidence_are_atomic() -> None:
    repo = MemoryExtensionRepo()
    chat, actor = ChatRef("tg", "-1001"), UserRef("tg", "7")
    artifact = Artifact("identity", "def run(arguments): return arguments", (ExampleCase({}, {}),))
    request_id = await repo.create_request(
        chat=chat, actor=actor, description="x", examples=artifact.cases
    )
    assert await repo.claim_build(chat=chat, request_id=request_id, actor=actor)
    assert not await repo.claim_build(chat=chat, request_id=request_id, actor=actor)
    revision_id = await repo.save_revision(
        chat=chat, request_id=request_id, actor=actor, artifact=artifact
    )
    assert not await repo.activate(
        chat=chat, revision_id=revision_id, reviewer=actor, digest=artifact.digest
    )
    await repo.save_evidence(
        chat=chat,
        revision_id=revision_id,
        digest=artifact.digest,
        passed=True,
        image_id="sha256:" + "a" * 64,
        policy_digest="b" * 64,
        harness_digest="c" * 64,
        case_count=1,
    )
    assert await repo.activate(
        chat=chat, revision_id=revision_id, reviewer=actor, digest=artifact.digest
    )
    assert await repo.get_activation(chat=chat, revision_id=revision_id) is not None


@pytest.mark.asyncio
async def test_run_epoch_suppresses_revoked_or_purged_result() -> None:
    repo = MemoryExtensionRepo()
    chat, actor = ChatRef("tg", "-1001"), UserRef("tg", "7")
    artifact = Artifact("identity", "def run(arguments): return arguments", (ExampleCase({}, {}),))
    req = await repo.create_request(
        chat=chat, actor=actor, description="x", examples=artifact.cases
    )
    await repo.claim_build(chat=chat, request_id=req, actor=actor)
    revision = await repo.save_revision(chat=chat, request_id=req, actor=actor, artifact=artifact)
    await repo.save_evidence(
        chat=chat,
        revision_id=revision,
        digest=artifact.digest,
        passed=True,
        image_id="sha256:" + "a" * 64,
        policy_digest="b" * 64,
        harness_digest="c" * 64,
        case_count=1,
    )
    await repo.activate(chat=chat, revision_id=revision, reviewer=actor, digest=artifact.digest)
    reserved = await repo.reserve_run(chat=chat, revision_id=revision, actor=actor, run_id="r1")
    assert reserved is not None
    await repo.revoke(chat=chat, revision_id=revision, reviewer=actor)
    result = RunResult("ok", {"secret": "not stored"}, 1, "sha256:" + "a" * 64, "b" * 64, "c" * 64)
    assert not await repo.record_run(
        chat=chat,
        revision_id=revision,
        run_id="r1",
        digest=artifact.digest,
        epoch=reserved[1],
        actor=actor,
        result=result,
    )
    assert await repo.get_activation(chat=chat, revision_id=revision) is None
    stored_run = repo._runs[(chat.key, "r1")]
    assert stored_run["status"] == "ok" and stored_run.get("completed_at") is not None


@pytest.mark.asyncio
async def test_late_run_after_suspension_finalizes_without_mutating_activation() -> None:
    from dataclasses import replace

    repo = MemoryExtensionRepo()
    chat, actor = ChatRef("tg", "-1001"), UserRef("tg", "7")
    artifact = Artifact("identity", "def run(arguments): return 1", (ExampleCase({}, 1),))
    request = await repo.create_request(
        chat=chat, actor=actor, description="x", examples=artifact.cases
    )
    await repo.claim_build(chat=chat, request_id=request, actor=actor)
    revision = await repo.save_revision(
        chat=chat, request_id=request, actor=actor, artifact=artifact
    )
    await repo.save_evidence(
        chat=chat,
        revision_id=revision,
        digest=artifact.digest,
        passed=True,
        image_id="sha256:" + "a" * 64,
        policy_digest="b" * 64,
        harness_digest="c" * 64,
        case_count=1,
    )
    await repo.activate(chat=chat, revision_id=revision, reviewer=actor, digest=artifact.digest)
    reserved = await repo.reserve_run(chat=chat, revision_id=revision, actor=actor, run_id="late")
    assert reserved is not None
    activation = repo._activations[(chat.key, revision)]
    repo._activations[(chat.key, revision)] = replace(activation, status="suspended", failures=3)

    accepted = await repo.record_run(
        chat=chat,
        revision_id=revision,
        run_id="late",
        digest=artifact.digest,
        epoch=reserved[1],
        actor=actor,
        result=RunResult("ok", 1, 1, "sha256:" + "a" * 64, "b" * 64, "c" * 64),
    )

    assert not accepted
    assert repo._runs[(chat.key, "late")].get("completed_at") is not None
    assert repo._activations[(chat.key, revision)].status == "suspended"
    assert repo._activations[(chat.key, revision)].failures == 3


@pytest.mark.asyncio
async def test_stale_epoch_failure_cannot_suspend_reactivated_revision() -> None:
    repo = MemoryExtensionRepo()
    chat, actor = ChatRef("tg", "-1001"), UserRef("tg", "7")
    artifact = Artifact("identity", "def run(arguments): return 1", (ExampleCase({}, 1),))
    request = await repo.create_request(
        chat=chat, actor=actor, description="x", examples=artifact.cases
    )
    await repo.claim_build(chat=chat, request_id=request, actor=actor)
    revision = await repo.save_revision(
        chat=chat, request_id=request, actor=actor, artifact=artifact
    )
    await repo.save_evidence(
        chat=chat,
        revision_id=revision,
        digest=artifact.digest,
        passed=True,
        image_id="sha256:" + "a" * 64,
        policy_digest="b" * 64,
        harness_digest="c" * 64,
        case_count=1,
    )
    await repo.activate(chat=chat, revision_id=revision, reviewer=actor, digest=artifact.digest)
    reserved = await repo.reserve_run(
        chat=chat, revision_id=revision, actor=actor, run_id="stale-epoch"
    )
    assert reserved is not None
    await repo.revoke(chat=chat, revision_id=revision, reviewer=actor)
    await repo.activate(chat=chat, revision_id=revision, reviewer=actor, digest=artifact.digest)

    accepted = await repo.record_run(
        chat=chat,
        revision_id=revision,
        run_id="stale-epoch",
        digest=artifact.digest,
        epoch=reserved[1],
        actor=actor,
        result=RunResult("execution_error", None, 2, "sha256:" + "a" * 64, "b" * 64, "c" * 64),
    )

    assert not accepted
    activation = await repo.get_activation(chat=chat, revision_id=revision)
    assert activation is not None and activation.failures == 0 and activation.epoch > reserved[1]
    assert repo._runs[(chat.key, "stale-epoch")].get("completed_at") is not None


@pytest.mark.asyncio
async def test_status_reports_scoped_monitoring_metadata_without_private_payloads() -> None:
    from datetime import UTC, datetime, timedelta

    repo = MemoryExtensionRepo()
    chat, foreign = ChatRef("tg", "-1001"), ChatRef("tg", "-1002")
    requester, builder = UserRef("tg", "7"), UserRef("tg", "8")
    artifact = Artifact(
        "secret description",
        "def run(arguments): return arguments",
        (ExampleCase({"secret": 1}, 1),),
    )
    request = await repo.create_request(
        chat=chat, actor=requester, description="secret description", examples=artifact.cases
    )
    await repo.claim_build(chat=chat, request_id=request, actor=builder)
    revision = await repo.save_revision(
        chat=chat, request_id=request, actor=builder, artifact=artifact
    )
    await repo.save_evidence(
        chat=chat,
        revision_id=revision,
        digest=artifact.digest,
        passed=True,
        image_id="sha256:" + "a" * 64,
        policy_digest="b" * 64,
        harness_digest="c" * 64,
        case_count=1,
    )
    await repo.activate(chat=chat, revision_id=revision, reviewer=builder, digest=artifact.digest)
    reserved = await repo.reserve_run(
        chat=chat, revision_id=revision, actor=requester, run_id="monitor"
    )
    assert reserved is not None
    await repo.record_run(
        chat=chat,
        revision_id=revision,
        run_id="monitor",
        digest=artifact.digest,
        epoch=reserved[1],
        actor=requester,
        result=RunResult("ok", {"secret_result": 1}, 2, "sha256:" + "a" * 64, "b" * 64, "c" * 64),
    )

    rows = await repo.status(chat=chat, viewer=requester, include_drafts=False)
    assert len(rows) == 1
    row = rows[0]
    assert row["revision_id"] == revision and row["status"] == "active"
    assert row["build_state"] == "built" and row["test_status"] == "passed"
    assert row["test_case_count"] == 1 and row["invocations"] == 1
    assert row["successful_runs"] == 1 and row["failed_runs"] == 0
    assert row["runs_today"] == 1 and row["run_limit"] == 100
    assert "requester_key" in row
    assert not {"source", "arguments", "value", "artifact", "description"} & row.keys()
    assert await repo.status(chat=foreign, viewer=requester, include_drafts=True) == []
    pending = await repo.reserve_run(
        chat=chat, revision_id=revision, actor=requester, run_id="stale-running"
    )
    assert pending is not None
    repo._runs[(chat.key, "stale-running")]["created_at"] = datetime.now(UTC) - timedelta(days=100)
    await repo.purge_older_than(datetime.now(UTC) - timedelta(days=90))
    assert (chat.key, "stale-running") not in repo._runs


@pytest.mark.asyncio
async def test_run_quota_is_atomic_at_scope_boundary() -> None:
    repo = MemoryExtensionRepo()
    chat, actor = ChatRef("tg", "-1001"), UserRef("tg", "7")
    artifact = Artifact("identity", "def run(arguments): return arguments", (ExampleCase({}, {}),))
    request = await repo.create_request(
        chat=chat, actor=actor, description="x", examples=artifact.cases
    )
    await repo.claim_build(chat=chat, request_id=request, actor=actor)
    revision = await repo.save_revision(
        chat=chat, request_id=request, actor=actor, artifact=artifact
    )
    await repo.save_evidence(
        chat=chat,
        revision_id=revision,
        digest=artifact.digest,
        passed=True,
        image_id="sha256:" + "a" * 64,
        policy_digest="b" * 64,
        harness_digest="c" * 64,
        case_count=1,
    )
    await repo.activate(chat=chat, revision_id=revision, reviewer=actor, digest=artifact.digest)
    results = await asyncio.gather(
        *(
            repo.reserve_run(chat=chat, revision_id=revision, actor=actor, run_id=f"r{index}")
            for index in range(105)
        )
    )
    assert sum(result is not None for result in results) == 100


@pytest.mark.asyncio
async def test_retention_preserves_supporting_records_for_approved_artifacts() -> None:
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    repo = MemoryExtensionRepo()
    chat, requester, builder = ChatRef("tg", "-1001"), UserRef("tg", "7"), UserRef("tg", "8")
    artifact = Artifact("x", "def run(arguments): return 1", (ExampleCase({}, 1),))
    retained = []
    for state in ("active", "suspended"):
        request = await repo.create_request(
            chat=chat, actor=requester, description="x", examples=artifact.cases
        )
        await repo.claim_build(chat=chat, request_id=request, actor=builder)
        revision = await repo.save_revision(
            chat=chat, request_id=request, actor=builder, artifact=artifact
        )
        await repo.save_evidence(
            chat=chat,
            revision_id=revision,
            digest=artifact.digest,
            passed=True,
            image_id="sha256:" + "a" * 64,
            policy_digest="b" * 64,
            harness_digest="c" * 64,
            case_count=1,
        )
        await repo.activate(
            chat=chat, revision_id=revision, reviewer=builder, digest=artifact.digest
        )
        if state == "suspended":
            activation = repo._activations[(chat.key, revision)]
            repo._activations[(chat.key, revision)] = replace(activation, status="suspended")
        old = datetime.now(UTC) - timedelta(days=100)
        repo._requests[(chat.key, request)] = replace(
            repo._requests[(chat.key, request)], created_at=old
        )
        repo._revisions[(chat.key, revision)] = replace(
            repo._revisions[(chat.key, revision)], created_at=old
        )
        retained.append((request, revision))

    await repo.purge_older_than(datetime.now(UTC) - timedelta(days=90))
    for request, revision in retained:
        assert await repo.get_request(chat=chat, request_id=request) is not None
        assert await repo.get_revision(chat=chat, revision_id=revision) is not None
        assert await repo.get_evidence(chat=chat, revision_id=revision) is not None
        assert await repo.status(chat=chat, revision_id=revision)


@pytest.mark.asyncio
async def test_purging_requester_removes_admin_built_private_artifact_and_suppresses_run() -> None:
    repo = MemoryExtensionRepo()
    chat = ChatRef("tg", "42")
    requester, builder, reviewer = UserRef("tg", "7"), UserRef("tg", "8"), UserRef("tg", "9")
    artifact = Artifact("private", "def run(arguments): return 1", (ExampleCase({}, 1),))
    request = await repo.create_request(
        chat=chat, actor=requester, description="private", examples=artifact.cases
    )
    await repo.claim_build(chat=chat, request_id=request, actor=builder)
    revision = await repo.save_revision(
        chat=chat, request_id=request, actor=builder, artifact=artifact
    )
    await repo.save_evidence(
        chat=chat,
        revision_id=revision,
        digest=artifact.digest,
        passed=True,
        image_id="sha256:" + "a" * 64,
        policy_digest="b" * 64,
        harness_digest="c" * 64,
        case_count=1,
    )
    await repo.activate(chat=chat, revision_id=revision, reviewer=reviewer, digest=artifact.digest)
    reserved = await repo.reserve_run(
        chat=chat, revision_id=revision, actor=builder, run_id="private-run"
    )
    assert reserved is not None

    await repo.purge_user(chat=chat, user=requester)

    assert await repo.get_request(chat=chat, request_id=request) is None
    assert await repo.get_revision(chat=chat, revision_id=revision) is None
    assert await repo.get_evidence(chat=chat, revision_id=revision) is None
    assert not await repo.record_run(
        chat=chat,
        revision_id=revision,
        run_id="private-run",
        digest=artifact.digest,
        epoch=reserved[1],
        actor=builder,
        result=RunResult("ok", 1, 1, "sha256:" + "a" * 64, "b" * 64, "c" * 64),
    )
