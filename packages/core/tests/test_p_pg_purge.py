"""Postgres purge integration checks; skipped when TEST_PG_DSN isn't reachable."""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import resources

import asyncpg
import pytest
from chatcore.attribution import user_turn_content
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.events import ChatRef, Mention
from chatcore.pending import PendingAction
from chatcore.store import migrations
from chatcore.store.pg import PgCoreRepo, PgDatabase
from chatcore.testing import GROUP, message, user

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")


@pytest.fixture
async def db():
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


async def test_pg_core_and_conversation_purges_are_scoped(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    repo = PgCoreRepo(db)
    chat_b = ChatRef("fake", "b")
    alice, bob = user("alice"), user("bob")
    alice_a, bob_a, alice_b = (
        message("alice a", sender=alice),
        message("bob a", sender=bob),
        message("alice b", sender=alice, chat=chat_b),
    )
    for incoming in (alice_a, bob_a, alice_b):
        await repo.log_message(incoming)
    store = ConversationStore(db)
    turn_a = await store.add(GROUP, None, "user", {"text": "alice"}, alice_a.message_id)
    await store.add(GROUP, None, "assistant", {"text": "reply"}, alice_a.message_id)
    turn_b = await store.add(chat_b, None, "user", {"text": "alice b"}, alice_b.message_id)
    await store.save_summary(GROUP, None, "quotes alice", turn_a.id)
    await store.save_summary(chat_b, None, "B summary", turn_b.id)
    await store.record_usage(GROUP, alice.ref, "fake", "fake", 1, 2)
    await store.record_usage(chat_b, alice.ref, "fake", "fake", 1, 2)
    plugin = ConversationPlugin(store)

    await plugin.purge_user(GROUP, alice.ref, type("StoresStub", (), {"db": db})())
    await repo.purge_user(GROUP, alice.ref)
    assert [row.text for row in await repo.recent_messages(GROUP, limit=10)] == ["bob a"]
    assert [row.text for row in await repo.recent_messages(chat_b, limit=10)] == ["alice b"]
    assert await store.recent(GROUP, None) == []
    assert await store.summary(GROUP, None) is None
    assert await store.recent(chat_b, None) == []
    assert await store.summary(chat_b, None) == ("B summary", turn_b.id)
    assert (
        await db.fetchval("SELECT count(*) FROM conversation_turns WHERE chat_key=$1", chat_b.key)
        == 1
    )
    assert await store.today_usage(GROUP) == 0
    assert await store.today_usage(chat_b) == 3

    await repo.purge_user(chat_b, alice.ref)
    assert [row.text for row in await repo.recent_messages(GROUP, limit=10)] == ["bob a"]
    await repo.purge_chat(chat_b)
    await plugin.purge_chat(chat_b, type("StoresStub", (), {"db": db})())
    assert await repo.recent_messages(chat_b, limit=10) == []
    assert [row.text for row in await repo.recent_messages(GROUP, limit=10)] == ["bob a"]
    assert await store.recent(chat_b, None) == []


async def test_pg_purge_older_than_uses_passed_clock(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    repo = PgCoreRepo(db)
    old = message("old")
    recent = message("recent")
    await repo.log_message(old)
    await repo.log_message(recent)
    await db.execute(
        "UPDATE messages SET sent_at=$1 WHERE message_id=$2",
        old.sent_at.replace(year=2020),
        old.message_id,
    )
    action = PendingAction.create(
        chat=GROUP, requester=user().ref, plugin="p", kind="x", payload={}, summary="s"
    )
    await repo.save_pending(replace(action, expires_at=action.created_at.replace(year=2020)))
    removed = await repo.purge_older_than(old.sent_at.replace(year=2021), now=old.sent_at)
    assert removed == 2
    assert [row.text for row in await repo.recent_messages(GROUP, limit=10)] == ["recent"]


async def test_pg_conversation_retention_is_chat_scoped(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    store = ConversationStore(db)
    chat_b = ChatRef("fake", "b")
    old = await store.add(GROUP, None, "user", {"text": "old A"})
    cutoff = datetime.now(UTC) - timedelta(days=365)
    old_time = cutoff - timedelta(days=400)
    await db.execute("UPDATE conversation_turns SET created_at=$1 WHERE id=$2", old_time, old.id)
    await store.reset(GROUP, None, by=user("alice").ref)
    recent = await store.add(GROUP, None, "user", {"text": "recent A"})
    other_recent = await store.add(chat_b, None, "user", {"text": "recent B"})
    await store.record_usage(GROUP, user("alice").ref, "fake", "fake", 1, 1)
    await store.record_usage(chat_b, user("alice").ref, "fake", "fake", 1, 1)
    await db.execute("UPDATE token_usage SET created_at=$1 WHERE chat_key=$2", old_time, GROUP.key)

    await ConversationPlugin(store).purge_older_than(cutoff, type("StoresStub", (), {"db": db})())
    assert [turn.id for turn in await store.recent(GROUP, None)] == [recent.id]
    assert await store.summary(GROUP, None) is None
    assert await store.today_usage(GROUP) == 0
    assert [turn.id for turn in await store.recent(chat_b, None)] == [other_recent.id]
    assert await db.fetchval("SELECT count(*) FROM conversation_turns WHERE id=$1", old.id) == 0
    assert await db.fetchval("SELECT count(*) FROM conversation_turns WHERE id=$1", recent.id) == 1
    assert await store.reset_point(GROUP, None) is not None
    assert await store.today_usage(chat_b) == 2


async def test_pg_forgetme_deletes_expired_source_turns_and_clears_mention_metadata(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    repo = PgCoreRepo(db)
    other = ChatRef("fake", "other")
    alice = replace(user("alice"), username="alice_old")
    bob = user("bob")
    own = message("own raw", sender=alice)
    by_id = replace(
        message("Alice was named", sender=bob),
        mentions=(Mention(0, 5, "text_mention", "Alice", alice.ref, "alice_old", "Alice"),),
    )
    by_handle = replace(
        message("@alice_old was named", sender=bob),
        mentions=(Mention(0, 10, "mention", "@alice_old", username="alice_old"),),
    )
    other_copy = replace(
        message("Alice elsewhere", sender=bob, chat=other),
        mentions=(Mention(0, 5, "text_mention", "Alice", alice.ref, "alice_old", "Alice"),),
    )
    for incoming in (own, by_id, by_handle, other_copy):
        await repo.log_message(incoming)
    await repo.upsert_member(GROUP, alice)
    store = ConversationStore(db)
    conversation = await store.create_conversation(GROUP, None, alice.ref)
    expired = message("expired raw", sender=alice)
    await store.add(
        GROUP,
        None,
        "user",
        user_turn_content(expired, "expired raw"),
        "expired-source",
        conversation_id=conversation.id,
    )
    await store.add(
        GROUP,
        None,
        "assistant",
        {"text": "linked assistant"},
        "expired-source",
        conversation_id=conversation.id,
    )
    bob_turn = await store.add(
        GROUP,
        None,
        "user",
        user_turn_content(by_id, "Alice was named"),
        by_id.message_id,
        conversation_id=conversation.id,
    )
    other_turn = await store.add(
        other,
        None,
        "user",
        user_turn_content(other_copy, "Alice elsewhere"),
        other_copy.message_id,
    )
    await store.save_summary(GROUP, None, "chat summary", 0)
    await store.save_conversation_summary(
        GROUP,
        conversation.id,
        "conversation summary",
        0,
        generation=conversation.reset_generation,
    )
    plugin = ConversationPlugin(store)

    await plugin.purge_user(GROUP, alice.ref, type("StoresStub", (), {"db": db})())
    await repo.purge_user(GROUP, alice.ref)

    remaining = await store.recent(GROUP, None)
    assert [turn.id for turn in remaining] == [bob_turn.id]
    assert remaining[0].content["text"] == "Alice was named"
    assert "mentions" not in remaining[0].content
    assert await store.summary(GROUP, None) is None
    assert await store.conversation_summary(GROUP, conversation.id) is None
    remaining_other = (await store.recent(other, None))[0]
    assert remaining_other.id == other_turn.id
    assert (
        remaining_other.content["mentions"]
        == user_turn_content(other_copy, "Alice elsewhere")["mentions"]
    )
    assert await repo.get_message(GROUP, own.message_id) is None
    assert (await repo.get_message(GROUP, by_id.message_id)).mentions == ()
    assert (await repo.get_message(GROUP, by_handle.message_id)).mentions == ()
    assert (await repo.get_message(other, other_copy.message_id)).mentions == other_copy.mentions
    assert alice.ref in [member.ref for member in await repo.find_members(GROUP)]
