"""Disposable-schema Postgres checks; no live Google or chat calls."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from importlib import resources
from uuid import uuid4

import asyncpg
import pytest
from chatcore.store import migrations
from chatcore.store.pg import PgDatabase
from chatcore_google.repository import GoogleRepository

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")


@pytest.fixture
async def google_db():
    try:
        admin = await asyncpg.connect(DSN, timeout=2)
    except (OSError, TimeoutError, asyncpg.PostgresError):
        pytest.skip("Local development Postgres is unavailable")
    schema = "google_" + uuid4().hex[:12]
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = None
    try:
        pool = await asyncpg.create_pool(
            DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        db = PgDatabase(pool)
        await migrations.apply(
            db,
            [
                ("core", resources.files("chatcore.migrations")),
                ("google", resources.files("chatcore_google.migrations")),
            ],
        )
        yield db
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_settings_and_reservations_survive_new_repository_and_reject_replay(google_db):
    repo = GoogleRepository(google_db)
    assert await repo.settings("fake:a") == (True, 0)
    await repo.configure("fake:a", False, 0)
    restarted = GoogleRepository(google_db)
    assert await restarted.settings("fake:a") == (False, 1)
    assert await restarted.settings("fake:b") == (True, 0)
    with pytest.raises(ValueError):
        await restarted.reserve("fake:a", "old", "fake:user", 1, 10, 0)
    await restarted.configure("fake:a", True, 1)
    await restarted.reserve("fake:a", "once", "fake:user", 1, 10, 2)
    with pytest.raises(ValueError, match="handled|uncertain"):
        await GoogleRepository(google_db).reserve("fake:a", "once", "fake:user", 1, 10, 2)


async def test_concurrent_daily_quota_is_atomic_and_scoped(google_db):
    repo = GoogleRepository(google_db)
    results = await asyncio.gather(
        *[repo.reserve("fake:a", f"action-{i}", "fake:user", 2, 3, 0) for i in range(2)],
        return_exceptions=True,
    )
    assert sum(isinstance(value, ValueError) for value in results) == 1
    await repo.reserve("fake:b", "other", "fake:user", 3, 3, 0)
    assert await google_db.fetchval("SELECT count(*) FROM google_actions") == 2


async def test_links_and_privacy_hooks_cannot_cross_chat_or_user(google_db):
    repo = GoogleRepository(google_db)
    await repo.link("fake:a", "file", "trip-a", "fake:alice")
    await repo.link("fake:b", "file", "trip-b", "fake:alice")
    await repo.reserve("fake:a", "a", "fake:alice", 1, 5, 0)
    await repo.reserve("fake:a", "b", "fake:bob", 1, 5, 0)
    assert await repo.linked_trip("fake:a", "file") == "trip-a"
    await repo.purge("fake:a", "fake:alice")
    assert await repo.linked_trip("fake:a", "file") is None
    assert await repo.linked_trip("fake:b", "file") == "trip-b"
    assert await google_db.fetchval("SELECT count(*) FROM google_actions") == 1
    await repo.purge_older_than(datetime.now(UTC) + timedelta(seconds=1))
    assert await google_db.fetchval("SELECT count(*) FROM google_actions") == 0
    await repo.purge("fake:b")
    assert await google_db.fetchval("SELECT count(*) FROM google_itinerary_links") == 0


async def test_edit_reservation_journal_is_atomic_replay_safe_and_retained_until_resolution(
    google_db,
):
    repo = GoogleRepository(google_db)
    await repo.reserve_edit(
        "fake:edit",
        "action-1",
        "fake:alice",
        0,
        10,
        0,
        kind="edit_doc",
        source_id="source-id",
    )
    with pytest.raises(ValueError, match="handled|uncertain"):
        await GoogleRepository(google_db).reserve_edit(
            "fake:edit",
            "action-1",
            "fake:alice",
            0,
            10,
            0,
            kind="edit_doc",
            source_id="source-id",
        )
    result = await repo.edit_result("fake:edit", "action-1")
    assert result["phase"] == "reserved" and result["destination_id"] is None

    for phase in ("destination_created", "shared", "applying", "uncertain"):
        await repo.update_edit_result(
            "fake:edit", "action-1", phase=phase, destination_id="destination-id"
        )
        result = await repo.edit_result("fake:edit", "action-1")
        assert result["phase"] == phase and result["destination_id"] == "destination-id"
    await repo.reserve_edit(
        "fake:edit",
        "action-2",
        "fake:alice",
        0,
        10,
        0,
        kind="edit_doc",
        source_id="source-id",
    )
    await repo.update_edit_result(
        "fake:edit", "action-2", phase="destination_created", destination_id="other-copy"
    )
    await repo.update_edit_result("fake:edit", "action-2", phase="partial", failed_stage="edit")

    old = datetime.now(UTC) - timedelta(days=10)
    await google_db.execute(
        "UPDATE google_actions SET created_at=$3 WHERE chat_key=$1 AND action_id=$2",
        "fake:edit",
        "action-1",
        old,
    )
    await google_db.execute(
        "UPDATE google_edit_results SET created_at=$3 WHERE chat_key=$1 AND action_id=$2",
        "fake:edit",
        "action-1",
        old,
    )
    await repo.purge_older_than(datetime.now(UTC) + timedelta(seconds=1))
    assert (await repo.edit_result("fake:edit", "action-1"))["phase"] == "uncertain"
    assert (await repo.edit_result("fake:edit", "action-2"))["phase"] == "partial"
    await repo.purge_older_than(
        datetime.now(UTC) + timedelta(seconds=1),
        unresolved_before=datetime.now(UTC) - timedelta(days=1),
    )
    assert await repo.edit_result("fake:edit", "action-1") is None


async def test_same_google_edit_action_is_atomically_reserved_once(google_db):
    repo = GoogleRepository(google_db)
    results = await asyncio.gather(
        *[
            repo.reserve_edit(
                "fake:edit-race",
                "same-action",
                "fake:alice",
                0,
                10,
                0,
                kind="edit_doc",
                source_id="source-id",
            )
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert (
        await google_db.fetchval(
            "SELECT count(*) FROM google_actions WHERE chat_key='fake:edit-race'"
        )
        == 1
    )
    assert (
        await google_db.fetchval(
            "SELECT count(*) FROM google_edit_results WHERE chat_key='fake:edit-race'"
        )
        == 1
    )


async def test_destination_creation_claim_is_durable_and_one_time(google_db):
    repo = GoogleRepository(google_db)
    await repo.reserve_edit(
        "fake:claim",
        "same-action",
        "fake:alice",
        0,
        10,
        0,
        kind="browser_edit_doc",
        source_id="source-id",
    )
    results = await asyncio.gather(
        repo.claim_destination_creation(
            "fake:claim",
            "same-action",
            kind="browser_edit_doc",
            source_id="source-id",
            revision=0,
        ),
        GoogleRepository(google_db).claim_destination_creation(
            "fake:claim",
            "same-action",
            kind="browser_edit_doc",
            source_id="source-id",
            revision=0,
        ),
        return_exceptions=True,
    )
    assert sum(value is True for value in results) == 1
    assert sum(isinstance(value, ValueError) for value in results) == 1

    with pytest.raises(ValueError, match="claim|already"):
        await GoogleRepository(google_db).claim_destination_creation(
            "fake:claim",
            "same-action",
            kind="browser_edit_doc",
            source_id="source-id",
            revision=0,
        )
    await repo.configure("fake:claim", True, 0)
    with pytest.raises(ValueError, match="settings"):
        await GoogleRepository(google_db).claim_destination_creation(
            "fake:claim",
            "other-action",
            kind="browser_edit_doc",
            source_id="source-id",
            revision=0,
        )


async def test_create_doc_reservation_allows_null_source_but_existing_edit_kinds_do_not(google_db):
    repo = GoogleRepository(google_db)
    await repo.reserve_edit(
        "fake:create",
        "create-action",
        "fake:alice",
        0,
        10,
        0,
        kind="create_doc",
        source_id=None,
    )
    result = await repo.edit_result("fake:create", "create-action")
    assert result["kind"] == "create_doc" and result["source_id"] is None
    assert await repo.claim_destination_creation(
        "fake:create",
        "create-action",
        kind="create_doc",
        source_id=None,
        revision=0,
    )
    with pytest.raises(ValueError, match="already|claimed"):
        await repo.claim_destination_creation(
            "fake:create",
            "create-action",
            kind="create_doc",
            source_id=None,
            revision=0,
        )
    await repo.reserve("fake:create", "bad-edit-action", "fake:alice", 0, 10, 0)
    with pytest.raises(asyncpg.CheckViolationError):
        await google_db.execute(
            "INSERT INTO google_edit_results(chat_key,action_id,kind,phase,source_id) "
            "VALUES('fake:create','bad-edit-action','edit_doc','reserved',NULL)"
        )
    with pytest.raises(ValueError, match="source"):
        await repo.reserve_edit(
            "fake:create",
            "edit-action",
            "fake:alice",
            0,
            10,
            0,
            kind="edit_doc",
            source_id=None,
        )


async def test_output_authority_survives_repository_restart_and_requires_exact_topic(google_db):
    repo = GoogleRepository(google_db)
    binding = {
        "owner_id": "owner-permission-id",
        "owner_email": "owner@example.com",
        "thread_id": None,
        "mode": "docx_import",
    }
    await repo.reserve_edit(
        "fake:output",
        "action-output",
        "fake:alice",
        0,
        10,
        0,
        kind="edit_doc",
        source_id="original-id",
        output_binding=binding,
    )
    await repo.update_edit_result(
        "fake:output",
        "action-output",
        phase="destination_created",
        destination_id="known-output-id",
    )
    await repo.update_edit_result("fake:output", "action-output", phase="applying")
    await repo.update_edit_result(
        "fake:output",
        "action-output",
        phase="uncertain",
        failed_stage="verification",
        error_category="text_mismatch",
    )
    now = datetime.now(UTC)
    await google_db.execute(
        "INSERT INTO pending_actions(id,chat_key,thread_id,requester_key,plugin,kind,payload,"
        "summary,reply_code,created_at,expires_at,used_at) "
        "VALUES($1,$2,NULL,'fake:alice','google','edit_doc','{}','summary','0000',$3,$4,$3)",
        "action-output",
        "fake:output",
        now,
        now + timedelta(hours=1),
    )
    await google_db.execute("DELETE FROM pending_actions WHERE id='action-output'")

    restarted = GoogleRepository(google_db)
    output = await restarted.output_destination("fake:output", None, "known-output-id")
    assert output["output_owner_id"] == binding["owner_id"]
    assert output["output_owner_email"] == binding["owner_email"]
    assert output["output_thread_id"] is None
    assert output["output_mode"] == "docx_import"
    assert output["phase"] == "uncertain"
    assert await google_db.fetchval("SELECT count(*) FROM pending_actions") == 0
    assert await restarted.output_destination("fake:output", "topic-a", "known-output-id") is None
    assert await restarted.output_destination("fake:other", None, "known-output-id") is None

    for topic, action, destination in (
        ("", "empty-topic", "empty-topic-id"),
        ("topic-a", "named-topic", "named-topic-id"),
    ):
        await restarted.reserve_edit(
            "fake:output",
            action,
            "fake:alice",
            0,
            10,
            0,
            kind="create_doc",
            source_id=None,
            output_binding={**binding, "thread_id": topic, "mode": "create"},
        )
        await restarted.update_edit_result(
            "fake:output", action, phase="destination_created", destination_id=destination
        )
    assert await restarted.output_destination("fake:output", None, "empty-topic-id") is None
    assert await restarted.output_destination("fake:output", "", "empty-topic-id") is not None
    assert (
        await restarted.output_destination("fake:output", "topic-a", "named-topic-id") is not None
    )
    assert await restarted.output_destination("fake:output", "", "named-topic-id") is None
    await restarted.purge("fake:output")
    assert await restarted.output_destination("fake:output", None, "known-output-id") is None


async def test_output_authority_denies_legacy_and_partial_bindings(google_db):
    repo = GoogleRepository(google_db)
    await repo.reserve_edit(
        "fake:binding",
        "legacy",
        "fake:alice",
        0,
        10,
        0,
        kind="edit_doc",
        source_id="source-id",
    )
    await repo.update_edit_result(
        "fake:binding", "legacy", phase="destination_created", destination_id="legacy-id"
    )
    await repo.update_edit_result("fake:binding", "legacy", phase="applying")
    await repo.update_edit_result("fake:binding", "legacy", phase="verified")
    assert await repo.output_destination("fake:binding", None, "legacy-id") is None

    with pytest.raises(asyncpg.CheckViolationError):
        await google_db.execute(
            "UPDATE google_edit_results SET output_owner_id='partial' "
            "WHERE chat_key='fake:binding' AND action_id='legacy'"
        )


async def test_memory_edit_privacy_purge_cascades_per_user_and_chat():
    repo = GoogleRepository()
    for chat, action, requester in (
        ("fake:one", "alice", "fake:alice"),
        ("fake:one", "bob", "fake:bob"),
        ("fake:two", "alice", "fake:alice"),
    ):
        await repo.reserve_edit(
            chat, action, requester, 0, 10, 0, kind="edit_doc", source_id="source-id"
        )
        await repo.update_edit_result(
            chat, action, phase="destination_created", destination_id="copy-id-" + action
        )
    await repo.purge("fake:one", "fake:alice")
    assert await repo.edit_result("fake:one", "alice") is None
    assert await repo.edit_result("fake:one", "bob") is not None
    assert await repo.edit_result("fake:two", "alice") is not None
    await repo.purge("fake:one")
    assert await repo.edit_result("fake:one", "bob") is None


async def test_browser_edit_result_listing_is_scoped_by_chat_and_requester():
    repo = GoogleRepository()
    for chat, action, requester, kind in (
        ("fake:one", "alice", "fake:alice", "browser_edit_doc"),
        ("fake:one", "bob", "fake:bob", "browser_edit_doc"),
        ("fake:two", "other-chat", "fake:alice", "browser_edit_doc"),
        ("fake:one", "api-edit", "fake:alice", "edit_doc"),
    ):
        await repo.reserve_edit(chat, action, requester, 0, 10, 0, kind=kind, source_id="source-id")
    assert [row["action_id"] for row in await repo.browser_edit_results("fake:one")] == [
        "alice",
        "bob",
    ]
    assert [
        row["action_id"] for row in await repo.browser_edit_results("fake:one", "fake:alice")
    ] == ["alice"]


async def test_browser_edit_result_listing_uses_requester_key_in_postgres(google_db):
    repo = GoogleRepository(google_db)
    for action, requester in (("alice", "fake:alice"), ("bob", "fake:bob")):
        await repo.reserve_edit(
            "fake:list", action, requester, 0, 10, 0, kind="browser_edit_doc", source_id="src"
        )
    rows = await GoogleRepository(google_db).browser_edit_results("fake:list", "fake:alice")
    assert [row["action_id"] for row in rows] == ["alice"]


async def test_privacy_purge_cascades_edit_results(google_db):
    repo = GoogleRepository(google_db)
    await repo.reserve_edit(
        "fake:edit",
        "action-2",
        "fake:alice",
        0,
        10,
        0,
        kind="browser_edit_doc",
        source_id="source-id",
    )
    await repo.update_edit_result(
        "fake:edit", "action-2", phase="destination_created", destination_id="copy-id"
    )
    await repo.purge("fake:edit", "fake:alice")
    assert await repo.edit_result("fake:edit", "action-2") is None
