from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

import asyncpg
import pytest
from chatcore.events import ChatRef, UserRef
from chatcore.store.pg import PgDatabase
from chatcore_extensions.models import Artifact, ExampleCase, RunResult
from chatcore_extensions.pg_repository import PostgresExtensionRepo

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_repo():
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("set TEST_PG_DSN to the documented local development database")
    try:
        admin = await asyncpg.connect(dsn)
    except Exception:
        pytest.skip("development PostgreSQL service is unavailable")
    schema = "ext_test_" + secrets.token_hex(6)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def set_schema(conn):
        await conn.execute(f'SET search_path TO "{schema}"')

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8, setup=set_schema)
    try:
        async with pool.acquire() as conn:
            sql = (
                Path(__file__).parents[1] / "src/chatcore_extensions/migrations/0001_extensions.sql"
            )
            await conn.execute(sql.read_text())
        yield PostgresExtensionRepo(PgDatabase(pool))
    finally:
        await pool.close()
        await admin.execute("DROP SCHEMA IF EXISTS " + f'"{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_postgres_repository_scoped_lifecycle_and_privacy(pg_repo) -> None:
    repo = pg_repo
    group_a, group_b = ChatRef("tg", "-1001"), ChatRef("tg", "-1002")
    actor = UserRef("tg", "7")
    artifact = Artifact("identity", "def run(arguments): return arguments", (ExampleCase({}, {}),))
    request = await repo.create_request(
        chat=group_a, actor=actor, description="x", examples=artifact.cases
    )
    assert await repo.get_request(chat=group_b, request_id=request) is None
    assert await repo.claim_build(chat=group_a, request_id=request, actor=actor)
    revision = await repo.save_revision(
        chat=group_a, request_id=request, actor=actor, artifact=artifact
    )
    await repo.save_evidence(
        chat=group_a,
        revision_id=revision,
        digest=artifact.digest,
        passed=True,
        image_id="sha256:" + "a" * 64,
        policy_digest="b" * 64,
        harness_digest="c" * 64,
        case_count=1,
    )
    assert await repo.activate(
        chat=group_a, revision_id=revision, reviewer=actor, digest=artifact.digest
    )
    reserved = await repo.reserve_run(
        chat=group_a, revision_id=revision, actor=actor, run_id="pg-run1"
    )
    assert reserved is not None
    result = RunResult(
        "ok", {"private": "not persisted"}, 1, "sha256:" + "a" * 64, "b" * 64, "c" * 64
    )
    assert await repo.record_run(
        chat=group_a,
        revision_id=revision,
        run_id="pg-run1",
        digest=artifact.digest,
        epoch=reserved[1],
        actor=actor,
        result=result,
    )
    assert await repo.list_active(chat=group_b) == []
    await repo.purge_chat(chat=group_a)
    assert await repo.get_revision(chat=group_a, revision_id=revision) is None


@pytest.mark.asyncio
async def test_postgres_build_quota_is_atomic_and_scope_specific(pg_repo) -> None:
    repo = pg_repo
    chat_a, chat_b = ChatRef("tg", "-1001"), ChatRef("tg", "-1002")
    actor = UserRef("tg", "7")
    cases = (ExampleCase({}, 1),)
    requests_a = [
        await repo.create_request(chat=chat_a, actor=actor, description="a", examples=cases)
        for _ in range(6)
    ]
    request_b = await repo.create_request(chat=chat_b, actor=actor, description="b", examples=cases)
    await asyncio.gather(
        *(
            repo.claim_build(chat=chat_a, request_id=request_id, actor=actor)
            for request_id in requests_a
        )
    )
    built_a = [
        await repo.get_request(chat=chat_a, request_id=request_id) for request_id in requests_a
    ]
    assert sum(item.state == "building" for item in built_a if item) == 5
    assert (await repo.get_request(chat=chat_b, request_id=request_b)).state == "requested"


@pytest.mark.asyncio
async def test_postgres_cross_scope_digest_and_purge_suppress_late_result(pg_repo) -> None:
    repo = pg_repo
    chat_a, chat_b = ChatRef("tg", "-1001"), ChatRef("tg", "-1002")
    member, reviewer = UserRef("tg", "7"), UserRef("tg", "8")
    artifact = Artifact("identity", "def run(arguments): return arguments", (ExampleCase({}, {}),))
    revisions = []
    for chat in (chat_a, chat_b):
        request = await repo.create_request(
            chat=chat, actor=member, description="x", examples=artifact.cases
        )
        assert await repo.claim_build(chat=chat, request_id=request, actor=reviewer)
        revision = await repo.save_revision(
            chat=chat, request_id=request, actor=reviewer, artifact=artifact
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
        assert await repo.activate(
            chat=chat, revision_id=revision, reviewer=reviewer, digest=artifact.digest
        )
        revisions.append(revision)
    assert await repo.get_revision(chat=chat_a, revision_id=revisions[1]) is None
    reserved = await repo.reserve_run(
        chat=chat_a, revision_id=revisions[0], actor=member, run_id="race-run"
    )
    assert reserved is not None
    await repo.purge_chat(chat=chat_a)
    result = RunResult("ok", None, 1, "sha256:" + "a" * 64, "b" * 64, "c" * 64)
    assert not await repo.record_run(
        chat=chat_a,
        revision_id=revisions[0],
        run_id="race-run",
        digest=artifact.digest,
        epoch=reserved[1],
        actor=member,
        result=result,
    )
    assert await repo.get_activation(chat=chat_b, revision_id=revisions[1]) is not None


@pytest.mark.asyncio
async def test_postgres_retention_preserves_support_for_active_and_suspended(pg_repo) -> None:
    from datetime import UTC, datetime, timedelta

    repo = pg_repo
    chat, requester, builder = ChatRef("tg", "-1001"), UserRef("tg", "7"), UserRef("tg", "8")
    artifact = Artifact("x", "def run(arguments): return 1", (ExampleCase({}, 1),))
    retained = []
    for status in ("active", "suspended"):
        request = await repo.create_request(
            chat=chat, actor=requester, description="x", examples=artifact.cases
        )
        assert await repo.claim_build(chat=chat, request_id=request, actor=builder)
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
        assert await repo.activate(
            chat=chat, revision_id=revision, reviewer=builder, digest=artifact.digest
        )
        if status == "suspended":
            await repo.db.execute(
                "UPDATE extension_activations SET status='suspended' "
                "WHERE chat_key=$1 AND revision_id=$2",
                chat.key,
                revision,
            )
        await repo.db.execute(
            "UPDATE extension_requests SET created_at=$3 WHERE chat_key=$1 AND id=$2",
            chat.key,
            request,
            datetime.now(UTC) - timedelta(days=100),
        )
        await repo.db.execute(
            "UPDATE extension_revisions SET created_at=$3 WHERE chat_key=$1 AND id=$2",
            chat.key,
            revision,
            datetime.now(UTC) - timedelta(days=100),
        )
        retained.append((request, revision))

    await repo.purge_older_than(datetime.now(UTC) - timedelta(days=90))
    for request, revision in retained:
        assert await repo.get_request(chat=chat, request_id=request) is not None
        assert await repo.get_revision(chat=chat, revision_id=revision) is not None
        assert await repo.get_evidence(chat=chat, revision_id=revision) is not None
        assert await repo.status(chat=chat, revision_id=revision)


@pytest.mark.asyncio
async def test_postgres_purge_requester_removes_admin_built_artifact_and_suppresses_run(
    pg_repo,
) -> None:
    repo = pg_repo
    chat = ChatRef("tg", "42")
    requester, builder, reviewer = UserRef("tg", "7"), UserRef("tg", "8"), UserRef("tg", "9")
    artifact = Artifact("private", "def run(arguments): return 1", (ExampleCase({}, 1),))
    request = await repo.create_request(
        chat=chat, actor=requester, description="private", examples=artifact.cases
    )
    assert await repo.claim_build(chat=chat, request_id=request, actor=builder)
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
    assert await repo.activate(
        chat=chat, revision_id=revision, reviewer=reviewer, digest=artifact.digest
    )
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


@pytest.mark.asyncio
async def test_postgres_run_finalizes_after_revoke_and_status_survives_repo_recreation(
    pg_repo,
) -> None:
    from datetime import UTC, datetime, timedelta

    repo = pg_repo
    chat, requester, builder = ChatRef("tg", "-1001"), UserRef("tg", "7"), UserRef("tg", "8")
    artifact = Artifact(
        "private", "def run(arguments): return arguments", (ExampleCase({"x": 1}, 1),)
    )
    request = await repo.create_request(
        chat=chat, actor=requester, description="private", examples=artifact.cases
    )
    assert await repo.claim_build(chat=chat, request_id=request, actor=builder)
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
    assert await repo.activate(
        chat=chat, revision_id=revision, reviewer=builder, digest=artifact.digest
    )
    stale = await repo.reserve_run(
        chat=chat, revision_id=revision, actor=requester, run_id="stale-epoch"
    )
    assert stale is not None
    await repo.revoke(chat=chat, revision_id=revision, reviewer=builder)
    assert await repo.activate(
        chat=chat, revision_id=revision, reviewer=builder, digest=artifact.digest
    )
    assert not await repo.record_run(
        chat=chat,
        revision_id=revision,
        run_id="stale-epoch",
        digest=artifact.digest,
        epoch=stale[1],
        actor=requester,
        result=RunResult("execution_error", None, 2, "sha256:" + "a" * 64, "b" * 64, "c" * 64),
    )
    current = await repo.get_activation(chat=chat, revision_id=revision)
    assert current is not None and current.failures == 0 and current.epoch > stale[1]
    suspended = await repo.reserve_run(
        chat=chat, revision_id=revision, actor=requester, run_id="suspended-run"
    )
    assert suspended is not None
    await repo.db.execute(
        "UPDATE extension_activations SET status='suspended',failures=3 "
        "WHERE chat_key=$1 AND revision_id=$2",
        chat.key,
        revision,
    )
    assert not await repo.record_run(
        chat=chat,
        revision_id=revision,
        run_id="suspended-run",
        digest=artifact.digest,
        epoch=suspended[1],
        actor=requester,
        result=RunResult("execution_error", None, 2, "sha256:" + "a" * 64, "b" * 64, "c" * 64),
    )
    assert await repo.db.fetchval(
        "SELECT completed_at IS NOT NULL FROM extension_runs "
        "WHERE chat_key=$1 AND run_id='suspended-run'",
        chat.key,
    )
    assert await repo.activate(
        chat=chat, revision_id=revision, reviewer=builder, digest=artifact.digest
    )
    reserved = await repo.reserve_run(
        chat=chat, revision_id=revision, actor=requester, run_id="monitor"
    )
    assert reserved is not None
    await repo.revoke(chat=chat, revision_id=revision, reviewer=builder)
    accepted = await repo.record_run(
        chat=chat,
        revision_id=revision,
        run_id="monitor",
        digest=artifact.digest,
        epoch=reserved[1],
        actor=requester,
        result=RunResult("ok", {"sensitive": 1}, 2, "sha256:" + "a" * 64, "b" * 64, "c" * 64),
    )
    assert not accepted
    await repo.db.execute(
        "UPDATE extension_runs SET created_at=now()-interval '100 days', completed_at=NULL "
        "WHERE chat_key=$1 AND run_id='monitor'",
        chat.key,
    )
    await repo.purge_older_than(datetime.now(UTC) - timedelta(days=90))
    assert (
        await repo.db.fetchval(
            "SELECT count(*) FROM extension_runs WHERE chat_key=$1 AND run_id='monitor'", chat.key
        )
        == 0
    )

    # Fresh repository object must report the same persistent scope metadata.
    active_request = await repo.create_request(
        chat=chat, actor=requester, description="monitor me", examples=artifact.cases
    )
    assert await repo.claim_build(chat=chat, request_id=active_request, actor=builder)
    active_revision = await repo.save_revision(
        chat=chat, request_id=active_request, actor=builder, artifact=artifact
    )
    await repo.save_evidence(
        chat=chat,
        revision_id=active_revision,
        digest=artifact.digest,
        passed=True,
        image_id="sha256:" + "a" * 64,
        policy_digest="b" * 64,
        harness_digest="c" * 64,
        case_count=1,
    )
    assert await repo.activate(
        chat=chat, revision_id=active_revision, reviewer=builder, digest=artifact.digest
    )
    fresh = PostgresExtensionRepo(repo.db)
    rows = await fresh.status(chat=chat, viewer=requester, include_drafts=False)
    row = next(row for row in rows if row["revision_id"] == active_revision)
    assert row["build_state"] == "built" and row["test_status"] == "passed"
    assert row["test_case_count"] == 1 and row["invocations"] == 0
    assert row["runs_today"] == 3 and row["builds_today"] == 2
    assert not {"source", "arguments", "value", "artifact", "description"} & row.keys()
