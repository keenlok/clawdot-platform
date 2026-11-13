"""Postgres integration tests. Run ``docker compose -f compose.dev.yaml up -d`` first; skipped if
``TEST_PG_DSN`` isn't reachable."""

import json
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import resources

import asyncpg
import pytest
from chatcore.display_time_store import DisplayTimeService, PgDisplayTimeStore
from chatcore.events import Attachment, AttachmentKind, ChatRef, Mention, UserRef
from chatcore.pending import PendingAction
from chatcore.store import migrations
from chatcore.store.pg import PgCoreRepo, PgDatabase
from chatcore.testing import GROUP, message, user

pytestmark = pytest.mark.integration

DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")


@pytest.fixture
async def db():
    """A fresh schema per test, so tests never see each other's tables."""
    schema = f"t_{uuid.uuid4().hex[:8]}"
    try:
        pool = await asyncpg.create_pool(
            DSN, min_size=1, max_size=2, timeout=3, server_settings={"search_path": schema}
        )
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"Postgres not available: {type(exc).__name__}")
    database = PgDatabase(pool)
    await database.execute(f"CREATE SCHEMA {schema}")
    try:
        yield database
    finally:
        await database.execute(f"DROP SCHEMA {schema} CASCADE")
        await database.close()


CORE = [("chatcore", resources.files("chatcore.migrations"))]


async def test_migrations_apply_once_and_detect_edits(db, tmp_path):
    assert await migrations.apply(db, CORE) == [
        "chatcore/0001_core.sql",
        "chatcore/0002_conversation.sql",
        "chatcore/0003_administration.sql",
        "chatcore/0004_access_restrictions.sql",
        "chatcore/0005_context_reset.sql",
        "chatcore/0006_conversation_threads.sql",
        "chatcore/0007_display_time.sql",
        "chatcore/0008_message_attribution.sql",
        "chatcore/0009_budget_admin.sql",
        "chatcore/0010_privacy_erasures.sql",
        "chatcore/0011_trusted_group_invites.sql",
        "chatcore/0012_output_photo_attribution.sql",
    ]
    assert await migrations.apply(db, CORE) == []

    plugin_dir = tmp_path / "mig"
    plugin_dir.mkdir()
    (plugin_dir / "0001_core.sql").write_text("CREATE TABLE ledger_x (id int);")
    assert await migrations.apply(db, [("ledger", plugin_dir)]) == ["ledger/0001_core.sql"]
    (plugin_dir / "0001_core.sql").write_text("CREATE TABLE ledger_x (id bigint);")
    with pytest.raises(migrations.MigrationError):
        await migrations.apply(db, [("ledger", plugin_dir)])


async def test_message_attribution_round_trip_and_legacy_insert(db):
    await migrations.apply(db, CORE)
    repo = PgCoreRepo(db)
    alice = replace(user("alice"), display_name="Alex", username="alice_old")
    incoming = replace(
        message("ask Alex", sender=alice),
        mentions=(Mention(4, 8, "text_mention", "Alex", user("bob").ref, "bob_old", "Alex"),),
    )
    await repo.log_message(incoming)
    await repo.upsert_member(GROUP, replace(alice, username="alice_new"))

    for stored in (
        await repo.get_message(GROUP, incoming.message_id),
        (await repo.recent_messages(GROUP, limit=1))[0],
    ):
        assert stored is not None
        assert stored.username == "alice_old"
        assert stored.mentions == incoming.mentions
        assert stored.text == incoming.text

    await db.execute(
        "INSERT INTO messages (chat_key,message_id,user_key,display_name,text,sent_at) "
        "VALUES ($1,$2,$3,$4,$5,now())",
        GROUP.key,
        "legacy",
        "fake:legacy",
        "Legacy",
        "old row",
    )
    legacy = await repo.get_message(GROUP, "legacy")
    assert legacy is not None and legacy.username is None and legacy.mentions == ()
    assert await migrations.apply(db, CORE) == []


async def test_message_attribution_migration_upgrades_existing_rows(db, tmp_path):
    previous_migrations = tmp_path / "previous-migrations"
    previous_migrations.mkdir()
    for entry in resources.files("chatcore.migrations").iterdir():
        if entry.name[:4].isdigit() and int(entry.name[:4]) <= 7:
            (previous_migrations / entry.name).write_text(entry.read_text("utf-8"))
    assert await migrations.apply(db, [("chatcore", previous_migrations)]) == [
        "chatcore/0001_core.sql",
        "chatcore/0002_conversation.sql",
        "chatcore/0003_administration.sql",
        "chatcore/0004_access_restrictions.sql",
        "chatcore/0005_context_reset.sql",
        "chatcore/0006_conversation_threads.sql",
        "chatcore/0007_display_time.sql",
    ]
    await db.execute(
        "INSERT INTO messages (chat_key,message_id,user_key,display_name,text,sent_at) "
        "VALUES ($1,$2,$3,$4,$5,now())",
        GROUP.key,
        "pre-attribution-row",
        "fake:alice",
        "Alex",
        "old message",
    )

    assert await migrations.apply(db, CORE) == [
        "chatcore/0008_message_attribution.sql",
        "chatcore/0009_budget_admin.sql",
        "chatcore/0010_privacy_erasures.sql",
        "chatcore/0011_trusted_group_invites.sql",
        "chatcore/0012_output_photo_attribution.sql",
    ]
    row = await db.fetchrow(
        "SELECT username,mentions FROM messages WHERE chat_key=$1 AND message_id=$2",
        GROUP.key,
        "pre-attribution-row",
    )

    assert row is not None and row["username"] is None
    mentions = row["mentions"]
    if isinstance(mentions, str):
        mentions = json.loads(mentions)
    assert mentions == []
    assert await migrations.apply(db, CORE) == []


async def test_core_repo_round_trip_and_pending_single_use(db):
    await migrations.apply(db, CORE)
    repo = PgCoreRepo(db)
    alice = user("alice")
    await repo.log_message(message("hello", sender=alice))
    await repo.upsert_member(GROUP, alice)
    assert [m.text for m in await repo.recent_messages(GROUP, limit=5)] == ["hello"]
    assert [m.ref for m in await repo.find_members(GROUP)] == [alice.ref]
    assert await repo.recent_messages(ChatRef("fake", "other"), limit=5) == []

    photo = Attachment("file-1", AttachmentKind.PHOTO, mime_type="image/jpeg", size_bytes=10)
    shared = message("look", sender=alice, attachments=[photo])
    await repo.log_message(shared)
    stored = await repo.get_message(GROUP, shared.message_id)
    assert stored and stored.attachments == (photo,)
    assert await repo.get_message(ChatRef("fake", "other"), shared.message_id) is None

    action = PendingAction.create(
        chat=GROUP,
        requester=alice.ref,
        plugin="ledger",
        kind="add",
        payload={"amount": 1},
        summary="s",
    )
    await repo.save_pending(action)
    assert await repo.get_pending(ChatRef("fake", "other"), action.id) is None
    found = await repo.find_pending_by_code(GROUP, alice.ref, action.reply_code)
    assert found and found.payload == {"amount": 1}
    assert await repo.claim_pending(GROUP, action.id) is not None
    assert await repo.claim_pending(GROUP, action.id) is None

    expired = replace(
        PendingAction.create(
            chat=GROUP, requester=alice.ref, plugin="ledger", kind="add", payload={}, summary="s"
        ),
        expires_at=action.created_at,
    )
    await repo.save_pending(expired)
    assert await repo.claim_pending(GROUP, expired.id) is None


async def test_new_erasure_queues_behind_queued_operation_and_promotes_in_order(db):
    await migrations.apply(db, CORE)
    repo = PgCoreRepo(db)
    first = await repo.create_erasure("op-a", GROUP, "user", "fake:a")
    second = await repo.create_erasure("op-b", GROUP, "user", "fake:b")
    assert (first["status"], second["status"]) == ("active", "queued")
    # A finished and was deleted, but B has not been promoted yet (or promotion crashed).
    await repo.delete_erasure("op-a")

    third = await repo.create_erasure("op-c", GROUP, "user", "fake:c")

    assert third["status"] == "queued"
    assert (await repo.promote_erasure(GROUP))["operation_id"] == "op-b"
    assert await repo.promote_erasure(GROUP) is None
    await repo.delete_erasure("op-b")
    assert (await repo.promote_erasure(GROUP))["operation_id"] == "op-c"


async def test_display_time_service_persists_preferences_and_scopes_resets_and_purges(db):
    await migrations.apply(db, CORE)
    first = DisplayTimeService(PgDisplayTimeStore(db))
    second = DisplayTimeService(PgDisplayTimeStore(db))
    alice = UserRef("tg", "42")
    group = ChatRef("tg", "-100")
    dm = ChatRef("tg", "42")

    await first.set_zone(group, alice, is_dm=False, zone="Asia/Singapore")
    await first.set_zone(dm, alice, is_dm=True, zone="Europe/London")
    assert await second.resolve(group, alice, is_dm=False) == ("Asia/Singapore", "group")
    assert await second.resolve(dm, alice, is_dm=True) == ("Europe/London", "personal")
    assert await second.resolve(ChatRef("tg", "-200"), alice, is_dm=False) == (
        "UTC",
        "fallback",
    )

    await second.reset(dm, alice, is_dm=True)
    assert await first.resolve(dm, alice, is_dm=True) == ("UTC", "fallback")
    assert await first.resolve(group, alice, is_dm=False) == ("Asia/Singapore", "group")

    await second.set_zone(dm, alice, is_dm=True, zone="Europe/London")
    assert await PgDisplayTimeStore(db).purge_user(dm, alice) == 1
    assert await first.resolve(dm, alice, is_dm=True) == ("UTC", "fallback")
    assert await first.resolve(group, alice, is_dm=False) == ("Asia/Singapore", "group")
    assert await PgDisplayTimeStore(db).purge_chat(group) == 1


async def test_output_photo_survives_repository_restart_and_requester_erasure(db):
    await migrations.apply(db, CORE)
    repo = PgCoreRepo(db)
    photo = Attachment("platform-photo", AttachmentKind.PHOTO, size_bytes=2048)
    await repo.log_bot_message(
        GROUP,
        "output",
        "Edited photo",
        thread_id="7",
        attachments=(photo,),
        attachment_requester_key=user("alice").ref.key,
    )
    await repo.log_bot_message(
        GROUP,
        "other-output",
        "Other photo",
        thread_id=None,
        attachments=(photo,),
        attachment_requester_key=user("bob").ref.key,
    )
    restarted = PgCoreRepo(db)
    stored = await restarted.get_message(GROUP, "output")
    assert stored.is_bot and stored.thread_id == "7"
    assert stored.attachments == (photo,)
    assert stored.attachment_requester_key == user("alice").ref.key
    assert await restarted.purge_user(GROUP, user("alice").ref) == 1
    assert await repo.get_message(GROUP, "output") is None
    assert await repo.get_message(GROUP, "other-output") is not None
    await repo.purge_older_than(datetime.now(UTC) + timedelta(days=1))
    assert await repo.get_message(GROUP, "other-output") is None
