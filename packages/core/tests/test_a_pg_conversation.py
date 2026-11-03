"""Postgres-backed conversation tests; skip cleanly without the integration service."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources

import asyncpg
import pytest
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.attribution import user_turn_content
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.events import ChatRef, UserRef
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message
from chatcore.store import migrations
from chatcore.store.base import StoredMessage, Stores
from chatcore.store.memory import MemoryCache
from chatcore.store.pg import PgCoreRepo, PgDatabase
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, message, user
from chatcore.threads import ThreadSettings

pytestmark = pytest.mark.integration

DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")


@pytest.fixture
async def db():
    schema = f"t_{uuid.uuid4().hex[:8]}"
    try:
        pool = await asyncpg.create_pool(
            DSN,
            min_size=1,
            max_size=2,
            timeout=3,
            server_settings={"search_path": schema},
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


async def test_conversation_store_postgres_scope_upsert_reset_and_usage(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    store = ConversationStore(db)
    chat_a, chat_b = ChatRef("fake", "a"), ChatRef("fake", "b")
    user_a = UserRef("fake", "alice")

    a_first = await store.add(chat_a, None, "user", {"text": "A first"}, "m1")
    a_second = await store.add(chat_a, None, "assistant", {"text": "A reply"}, "m2")
    a_topic = await store.add(chat_a, "topic", "user", {"text": "topic only"}, "m3")
    b_first = await store.add(chat_b, None, "user", {"text": "B private"}, "m4")

    assert [row.id for row in await store.recent(chat_a, None)] == [a_first.id, a_second.id]
    assert [row.id for row in await store.recent(chat_a, "topic")] == [a_topic.id]
    assert [row.id for row in await store.recent(chat_b, None)] == [b_first.id]

    await store.save_summary(chat_a, None, "first summary", a_first.id)
    await store.save_summary(chat_a, None, "second summary", a_second.id)
    assert await store.summary(chat_a, None) == ("second summary", a_second.id)

    await store.record_usage(chat_a, user_a, "fake", "fake-model", 10, 5)
    assert await store.today_usage(chat_a) == 15
    assert await store.today_usage(chat_b) == 0
    await store.reset(chat_a, None)
    assert await store.recent(chat_a, None) == []
    assert await store.summary(chat_a, None) is None
    assert [row.id for row in await store.recent(chat_a, "topic")] == [a_topic.id]
    assert [row.id for row in await store.recent(chat_b, None)] == [b_first.id]


async def test_postgres_conversation_turn_preserves_attribution_json(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    store = ConversationStore(db)
    incoming = message("@bot ask Alex", sender=user("alice"), mention=True)
    content = user_turn_content(incoming, "ask Alex")
    created = await store.add(
        incoming.chat,
        incoming.thread_id,
        "user",
        content,
        incoming.message_id,
    )

    [saved] = await store.recent(incoming.chat, incoming.thread_id)

    assert saved.id == created.id
    assert saved.content == content


async def test_postgres_reset_point_persists_and_filters_retained_turns(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    chat = ChatRef("fake", "reset")
    actor = UserRef("fake", "alice")
    store = ConversationStore(db)
    old = await store.add(chat, "topic", "user", {"text": "before reset"}, "old-message")
    await store.save_summary(chat, "topic", "old summary", old.id)

    point = await store.reset(chat, "topic", by=actor)
    assert point.generation == 1
    restarted = ConversationStore(db)
    assert await restarted.reset_point(chat, "topic") == point
    assert await restarted.recent(chat, "topic") == []
    assert await restarted.summary(chat, "topic") is None
    assert (
        await db.fetchval("SELECT count(*) FROM conversation_turns WHERE chat_key=$1", chat.key)
        == 1
    )

    later = await restarted.add(chat, "topic", "user", {"text": "after reset"}, "new-message")
    await restarted.save_summary(chat, "topic", "raced pre-reset summary", old.id)
    assert await restarted.summary(chat, "topic") is None
    assert [turn.id for turn in await restarted.unsummarized(chat, "topic")] == [later.id]
    assert [turn.id for turn in await restarted.recent(chat, "topic")] == [later.id]

    other = ChatRef("fake", "purge")
    await restarted.add(other, None, "user", {"text": "purged"}, "purged")
    await restarted.reset(other, None, by=actor)
    await restarted.purge_chat(other)
    assert await restarted.reset_point(other, None) is None

    second_point = await restarted.reset(chat, "topic", by=actor)
    assert second_point.generation == 2
    assert await restarted.recent(chat, "topic") == []

    await restarted.purge_older_than(datetime.now(UTC) + timedelta(seconds=1))
    assert await restarted.reset_point(chat, "topic") is None


async def test_postgres_ambient_only_compaction_keeps_summary_visible(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    chat = ChatRef("fake", "ambient")
    actor = UserRef("fake", "alice")
    store = ConversationStore(db)
    point = await store.reset(chat, None, by=actor)
    ambient = StoredMessage(
        chat,
        "ambient-1",
        actor,
        "Alice",
        "new ambient context",
        point.reset_at + timedelta(seconds=1),
    )
    provider = ScriptedProvider([Completion(Message.assistant("ambient summary"), "end_turn")])

    await store.compact(chat, None, provider, "rules", "fake-model", force=True, ambient=[ambient])

    summary = await store.summary(chat, None)
    assert summary == ("ambient summary", point.after_turn_id)
    later = await store.add(chat, None, "user", {"text": "next turn"})
    assert [turn.id for turn in await store.recent(chat, None)] == [later.id]


async def test_postgres_retention_keeps_summary_after_reset_point_expires(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    chat = ChatRef("fake", "retained-summary")
    store = ConversationStore(db)
    reset_at = datetime(2020, 1, 1, tzinfo=UTC)
    point = await store.reset(chat, None, by=UserRef("fake", "alice"), at=reset_at)
    for index in range(6):
        await store.add(chat, None, "user", {"text": f"turn-{index}"})
    provider = ScriptedProvider([Completion(Message.assistant("retained summary"), "end_turn")])
    await store.compact(chat, None, provider, "rules", "fake-model", force=True, keep=4)
    expected = await store.summary(chat, None)
    assert expected is not None
    assert await store.reset_point(chat, None) == point

    await store.purge_older_than(reset_at + timedelta(days=1))

    assert await store.reset_point(chat, None) is None
    assert await store.summary(chat, None) == expected
    assert [turn.content["text"] for turn in await store.recent(chat, None)] == [
        "turn-2",
        "turn-3",
        "turn-4",
        "turn-5",
    ]


async def test_postgres_thread_conversations_scope_reopen_summaries_and_purges(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    store = ConversationStore(db)
    chat_a, chat_b = ChatRef("fake", "thread-a"), ChatRef("fake", "thread-b")
    alice, bob = UserRef("fake", "alice"), UserRef("fake", "bob")
    now = datetime.now(UTC)
    a = await store.create_conversation(chat_a, "topic", alice, at=now)
    b = await store.create_conversation(chat_a, "topic", bob, at=now)
    isolated = await store.create_conversation(chat_b, "topic", alice, at=now)
    assert a.id != b.id != isolated.id
    assert (
        await store.latest_conversation(chat_a, "topic", alice, since=now - timedelta(minutes=1))
    ).id == a.id
    assert await store.latest_conversation(chat_a, "topic", bob, since=now - timedelta(days=1)) == b
    assert await store.get_conversation(chat_b, a.id) is None

    await store.map_messages(
        chat_a,
        a.id,
        ["part-1", "part-2", "part-3"],
        primary_message_id="part-3",
        is_bot=True,
        user=None,
    )
    assert await store.conversation_for_message(chat_a, "part-1") == (a, "part-3")
    assert await store.conversation_for_message(chat_b, "part-1") is None
    first = await store.add(
        chat_a, "topic", "user", {"text": "old"}, "user-1", conversation_id=a.id
    )
    second = await store.add(
        chat_a, "topic", "assistant", {"text": "new"}, "bot-1", conversation_id=a.id
    )
    await store.save_conversation_summary(chat_a, a.id, "summary", first.id, generation=0)
    restarted = ConversationStore(db)
    assert await restarted.conversation_summary(chat_a, a.id) == ("summary", first.id)
    assert [turn.id for turn in await restarted.recent_conversation_turns(chat_a, a.id, 10)] == [
        second.id
    ]
    assert [
        turn.content["text"] for turn in await restarted.recent_conversation_turns(chat_a, a.id, 10)
    ] == ["new"]
    assert [
        turn.content["text"]
        for turn in await restarted.unsummarized_conversation_turns(chat_a, a.id)
    ] == ["new"]
    assert [
        turn.id for turn in await restarted.recent_conversation_turns(chat_b, isolated.id, 10)
    ] == []

    await restarted.reset(chat_a, "topic", by=alice)
    assert await restarted.latest_conversation(chat_a, "topic", alice) is None
    assert await restarted.conversation_summary(chat_a, a.id) is None
    assert await restarted.conversation_for_message(chat_a, "part-2") is not None

    await restarted.save_conversation_summary(
        chat_a, b.id, "remove with user purge", 0, generation=0
    )
    await restarted.map_messages(
        chat_a, b.id, ["bob-message"], primary_message_id="bob-message", is_bot=False, user=bob
    )
    await restarted.purge_user(chat_a, bob, {"bob-message"})
    assert await restarted.conversation_for_message(chat_a, "bob-message") is None
    assert bob.key not in (await restarted.get_conversation(chat_a, b.id)).participants
    assert await restarted.conversation_summary(chat_a, b.id) is None

    old = await restarted.create_conversation(
        chat_a, "old", alice, at=datetime(2020, 1, 1, tzinfo=UTC)
    )
    await restarted.add(
        chat_a,
        "old",
        "user",
        {"text": "new turn under expired conversation"},
        "recent-turn",
        conversation_id=old.id,
    )
    await restarted.map_messages(
        chat_a, old.id, ["old-part"], primary_message_id="old-part", is_bot=True, user=None
    )
    await db.execute(
        "UPDATE conversation_messages SET created_at=$1 WHERE chat_key=$2 AND message_id=$3",
        datetime(2020, 1, 1, tzinfo=UTC),
        chat_a.key,
        "old-part",
    )
    await restarted.purge_older_than(datetime(2021, 1, 1, tzinfo=UTC))
    assert await restarted.get_conversation(chat_a, old.id) is None
    assert await restarted.conversation_for_message(chat_a, "old-part") is None
    assert await restarted.recent_conversation_turns(chat_a, old.id, 10) == []
    await restarted.purge_chat(chat_a)
    assert await restarted.get_conversation(chat_a, b.id) is None
    assert await restarted.get_conversation(chat_b, isolated.id) is not None


async def test_postgres_retention_keeps_reset_generation_for_recent_conversation(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    store = ConversationStore(db)
    chat = ChatRef("fake", "retained-thread")
    alice = UserRef("fake", "alice")
    await store.reset(chat, "topic", by=alice, at=datetime(2020, 1, 1, tzinfo=UTC))
    conversation = await store.create_conversation(chat, "topic", alice)
    await store.save_conversation_summary(
        chat, conversation.id, "retained summary", 0, generation=1
    )

    await store.purge_older_than(datetime(2021, 1, 1, tzinfo=UTC))

    assert await store.latest_conversation(chat, "topic", alice) == conversation
    assert await store.conversation_summary(chat, conversation.id) == ("retained summary", 0)


async def test_agent_reads_postgres_conversation_turns_on_second_turn(db):
    await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
    stores = Stores(repo=PgCoreRepo(db), cache=MemoryCache(), db=db)
    store = ConversationStore(db)
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("first answer"), "end_turn"),
            Completion(Message.assistant("second answer"), "end_turn"),
        ]
    )
    limits = Limits(stores.cache)
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(recent_context_messages=1),
        limits=limits,
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store)],
        responder=responder,
        bot_name="bot",
        group_mentions_only=True,
        conversation_threads=ThreadSettings(),
    )
    sender = user("pg-alice")

    await app.handle(message("@bot first request text", sender=sender, mention=True))
    await app.handle(message("@bot second request", sender=sender, mention=True))

    assert len(provider.calls) == 2
    assert any("first request text" in item.text for item in provider.calls[1]["messages"])
