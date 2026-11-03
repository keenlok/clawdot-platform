from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.events import ChatRef, MessageRef
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message, user
from chatcore.threads import ThreadSettings, current_conversation, resolve_conversation


async def test_conversation_store_continues_each_participant_and_resolves_replies():
    store = ConversationStore()
    alice = user("alice")
    bob = user("bob")
    start = datetime(2026, 10, 31, tzinfo=UTC)

    alice_conversation = await store.create_conversation(GROUP, "topic", alice.ref, at=start)
    await store.map_messages(
        GROUP,
        alice_conversation.id,
        ["bot-1", "bot-2"],
        primary_message_id="bot-1",
        is_bot=True,
        user=None,
    )
    bob_conversation = await store.create_conversation(GROUP, "topic", bob.ref, at=start)

    assert alice_conversation.id != bob_conversation.id
    assert (
        await store.latest_conversation(
            GROUP, "topic", alice.ref, since=start - timedelta(minutes=1)
        )
        == alice_conversation
    )
    assert await store.conversation_for_message(GROUP, "bot-2") == (
        alice_conversation,
        "bot-1",
    )
    assert await store.conversation_for_message(ChatRef("fake", "other"), "bot-2") is None


async def test_reset_makes_conversations_unreachable_and_purges_mappings():
    store = ConversationStore()
    alice = user("alice")
    conversation = await store.create_conversation(GROUP, None, alice.ref)
    await store.map_messages(
        GROUP,
        conversation.id,
        ["human-1"],
        primary_message_id="human-1",
        is_bot=False,
        user=alice.ref,
    )

    await store.reset(GROUP, None, by=alice.ref)

    assert await store.latest_conversation(GROUP, None, alice.ref) is None
    assert await store.conversation_for_message(GROUP, "human-1") is not None
    await store.purge_user(GROUP, alice.ref, {"human-1"})
    assert await store.conversation_for_message(GROUP, "human-1") is None


async def test_unmapped_reply_to_unlogged_bot_message_starts_new_conversation():
    store = ConversationStore()
    stores = memory_stores()
    alice = user("alice")
    active = await store.create_conversation(GROUP, "topic", alice.ref)
    incoming = message(
        "continue from that answer",
        sender=alice,
        reply_to_bot=True,
        thread_id="topic",
    )

    resolved, reopened = await resolve_conversation(store, stores.repo, incoming)

    assert not reopened
    assert resolved.id != active.id


async def test_human_reply_ancestor_beats_askers_recent_conversation():
    stores, store = memory_stores(), ConversationStore()
    alice, bob, carol = user("alice"), user("bob"), user("carol")
    origin = message("I call Bob Captain", sender=alice, thread_id="topic")
    bridge = replace(message("yes", sender=bob, thread_id="topic"), reply_to=origin.ref)
    second_bridge = replace(
        message("right", sender=user("dave"), thread_id="topic"), reply_to=bridge.ref
    )
    ask = replace(
        message("@bot who calls whom Captain?", sender=carol, mention=True, thread_id="topic"),
        reply_to=second_bridge.ref,
    )
    for incoming in (origin, bridge, second_bridge):
        await stores.repo.log_message(incoming)
    wanted = await store.create_conversation(GROUP, "topic", alice.ref)
    await store.map_messages(
        GROUP,
        wanted.id,
        [origin.message_id],
        primary_message_id=origin.message_id,
        is_bot=False,
        user=alice.ref,
    )
    unrelated = await store.create_conversation(GROUP, "topic", carol.ref)

    found, reopened = await resolve_conversation(store, stores.repo, ask)

    assert found.id == wanted.id != unrelated.id
    assert reopened


async def test_bot_app_attributes_retagged_reply_to_original_not_askers_thread():
    stores = memory_stores()
    store = ConversationStore()
    alice = replace(user("alice"), display_name="Alex", username="alice_old")
    bob = replace(user("bob"), display_name="Alex", username="bob_old")
    carol = replace(user("carol"), display_name="Alex", username="carol_old")
    unrelated = await store.create_conversation(GROUP, "topic", carol.ref)
    await store.add(
        GROUP,
        "topic",
        "user",
        {"text": "Carol unrelated private thread"},
        "carol-unrelated",
        conversation_id=unrelated.id,
    )
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("Alice answer"), "end_turn"),
            Completion(Message.assistant("Correct reply"), "end_turn"),
        ]
    )
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(recent_context_messages=1),
        limits=Limits(stores.cache),
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
    origin = message("@bot I call Bob Captain", sender=alice, mention=True, thread_id="topic")
    await app.handle(origin)
    assert len(provider.calls) == 1

    bridge = replace(message("yes, Captain", sender=bob, thread_id="topic"), reply_to=origin.ref)
    await app.handle(bridge)
    assert len(provider.calls) == 1
    assert await store.conversation_for_message(GROUP, bridge.message_id) is None

    ask_text = "@bot who calls whom Captain?"
    ask = replace(
        message(ask_text, sender=carol, mention=True, thread_id="topic"),
        reply_to=bridge.ref,
    )
    await app.handle(ask)

    assert len(provider.calls) == 2
    carol_prompt = "\n".join(part.text for part in provider.calls[-1]["messages"])
    alice_conversation = await store.latest_conversation(GROUP, "topic", alice.ref)
    assert alice_conversation is not None
    assert current_conversation.get() is None
    assert '"user_key":"fake:alice"' in carol_prompt
    assert '"username":"alice_old"' in carol_prompt
    assert '"user_key":"fake:bob"' in carol_prompt
    assert '"username":"bob_old"' in carol_prompt
    assert '"user_key":"fake:carol"' in carol_prompt
    assert '"username":"carol_old"' in carol_prompt
    assert "I call Bob Captain" in carol_prompt
    assert "yes, Captain" in carol_prompt
    assert "Carol unrelated private thread" not in carol_prompt
    assert ask_text not in carol_prompt
    assert "who calls whom Captain?" in carol_prompt


async def test_direct_mapping_is_authoritative_when_alias_primary_row_is_missing():
    stores, store = memory_stores(), ConversationStore()
    alice, carol = user("alice"), user("carol")
    wanted = await store.create_conversation(GROUP, "topic", alice.ref)
    await store.map_messages(
        GROUP,
        wanted.id,
        ["alias-part"],
        primary_message_id="missing-primary",
        is_bot=True,
        user=None,
    )
    unrelated = await store.create_conversation(GROUP, "topic", carol.ref)
    ask = replace(
        message("@bot continue", sender=carol, mention=True, thread_id="topic"),
        reply_to=MessageRef(GROUP, "alias-part", "topic"),
    )

    found, reopened = await resolve_conversation(store, stores.repo, ask)

    assert found.id == wanted.id != unrelated.id
    assert reopened


async def test_force_new_bypasses_reply_mapping_and_message_reads():
    stores, store = memory_stores(), ConversationStore()
    alice = user("alice")
    ask = replace(
        message("@bot new", sender=alice, mention=True, thread_id="topic"),
        reply_to=MessageRef(GROUP, "alias-part", "topic"),
    )
    mapping_read = AsyncMock(wraps=store.conversation_for_message)
    row_read = AsyncMock(wraps=stores.repo.get_message)
    store.conversation_for_message = mapping_read
    stores.repo.get_message = row_read

    conversation, reopened = await resolve_conversation(store, stores.repo, ask, force_new=True)

    assert not reopened
    assert conversation.id == 1
    mapping_read.assert_not_awaited()
    row_read.assert_not_awaited()


async def test_reply_ancestry_cycle_and_depth_exhaustion_fall_back_at_32_reads():
    stores, store = memory_stores(), ConversationStore()
    alice = user("alice")
    active = await store.create_conversation(GROUP, "topic", alice.ref)
    cycle_a = replace(
        message("cycle a", sender=user("bob"), thread_id="topic"),
        message_id="cycle-a",
        reply_to=MessageRef(GROUP, "cycle-b", "topic"),
    )
    cycle_b = replace(
        message("cycle b", sender=user("bob"), thread_id="topic"),
        message_id="cycle-b",
        reply_to=MessageRef(GROUP, "cycle-a", "topic"),
    )
    await stores.repo.log_message(cycle_a)
    await stores.repo.log_message(cycle_b)
    mapping_read = AsyncMock(wraps=store.conversation_for_message)
    row_read = AsyncMock(wraps=stores.repo.get_message)
    store.conversation_for_message = mapping_read
    stores.repo.get_message = row_read
    ask = replace(
        message("@bot continue", sender=alice, mention=True, thread_id="topic"),
        reply_to=MessageRef(GROUP, "cycle-a", "topic"),
    )

    found, reopened = await resolve_conversation(store, stores.repo, ask)

    assert found.id == active.id
    assert not reopened
    assert mapping_read.await_count == 2
    assert row_read.await_count == 2

    chain = []
    for index in range(33):
        parent = f"hop-{index + 1}" if index < 32 else None
        row = message(f"hop {index}", sender=user("bob"), thread_id="topic")
        row = replace(
            row,
            message_id=f"hop-{index}",
            reply_to=MessageRef(GROUP, parent, "topic") if parent else None,
        )
        chain.append(row)
        await stores.repo.log_message(row)
    mapping_read.reset_mock()
    row_read.reset_mock()
    deep_ask = replace(ask, reply_to=MessageRef(GROUP, "hop-0", "topic"))

    found, reopened = await resolve_conversation(store, stores.repo, deep_ask)

    assert found.id == active.id
    assert not reopened
    assert mapping_read.await_count == 32
    assert row_read.await_count == 32


async def test_missing_parent_falls_back_but_cross_chat_and_other_topic_start_fresh():
    stores, store = memory_stores(), ConversationStore()
    alice = user("alice")
    active = await store.create_conversation(GROUP, "topic", alice.ref)
    ask = replace(
        message("@bot continue", sender=alice, mention=True, thread_id="topic"),
        reply_to=MessageRef(GROUP, "deleted", "topic"),
    )
    missing, reopened = await resolve_conversation(store, stores.repo, ask)
    assert missing.id == active.id and not reopened

    cross_chat_ask = replace(ask, reply_to=MessageRef(ChatRef("fake", "other"), "remote"))
    mapping_read = AsyncMock(wraps=store.conversation_for_message)
    row_read = AsyncMock(wraps=stores.repo.get_message)
    store.conversation_for_message = mapping_read
    stores.repo.get_message = row_read
    cross_chat, _ = await resolve_conversation(store, stores.repo, cross_chat_ask)
    assert cross_chat.id != active.id
    mapping_read.assert_not_awaited()
    row_read.assert_not_awaited()

    other_topic_row = replace(
        message("other topic", sender=user("bob"), thread_id="other-topic"),
        message_id="other-topic-row",
    )
    await stores.repo.log_message(other_topic_row)
    topic_ask = replace(ask, reply_to=MessageRef(GROUP, other_topic_row.message_id))
    fresh, reopened = await resolve_conversation(store, stores.repo, topic_ask)
    assert fresh.id != active.id and not reopened

    other_topic_conversation = await store.create_conversation(
        GROUP, "other-topic", user("bob").ref
    )
    await store.map_messages(
        GROUP,
        other_topic_conversation.id,
        ["other-topic-alias"],
        primary_message_id="missing-primary",
        is_bot=False,
        user=user("bob").ref,
    )
    mapped_topic_ask = replace(ask, reply_to=MessageRef(GROUP, "other-topic-alias"))
    fresh, reopened = await resolve_conversation(store, stores.repo, mapped_topic_ask)
    assert fresh.id != active.id and not reopened

    await stores.repo.log_bot_message(
        GROUP, "unmapped-bot", "unmapped bot reply", thread_id="topic"
    )
    bot_ask = replace(ask, reply_to=MessageRef(GROUP, "unmapped-bot"))
    fresh, reopened = await resolve_conversation(store, stores.repo, bot_ask)
    assert fresh.id != active.id and not reopened


async def test_pre_reset_row_and_old_generation_mapping_are_hard_boundaries():
    stores, store = memory_stores(), ConversationStore()
    alice, bob = user("alice"), user("bob")
    old_time = datetime(2026, 10, 1, tzinfo=UTC)
    reset_time = datetime(2026, 10, 2, tzinfo=UTC)
    old_row = replace(
        message("before reset", sender=bob, thread_id="topic"),
        message_id="pre-reset",
        sent_at=old_time,
    )
    await stores.repo.log_message(old_row)
    active = await store.create_conversation(GROUP, "topic", alice.ref, at=reset_time)
    old_conversation = await store.create_conversation(GROUP, "topic", bob.ref, at=old_time)
    await store.map_messages(
        GROUP,
        old_conversation.id,
        ["old-generation-map"],
        primary_message_id="missing-primary",
        is_bot=False,
        user=bob.ref,
    )
    await store.reset(GROUP, "topic", by=alice.ref, at=reset_time)
    ask = replace(
        message("@bot continue", sender=alice, mention=True, thread_id="topic"),
        reply_to=MessageRef(GROUP, old_row.message_id),
        sent_at=reset_time + timedelta(seconds=1),
    )

    fresh, reopened = await resolve_conversation(store, stores.repo, ask)

    assert fresh.id != active.id and not reopened

    stale_ask = replace(ask, reply_to=MessageRef(GROUP, "old-generation-map"))
    fresh, reopened = await resolve_conversation(store, stores.repo, stale_ask)
    assert fresh.id not in {active.id, old_conversation.id} and not reopened


async def test_app_keeps_parallel_conversation_turns_separate():
    stores = memory_stores()
    store = ConversationStore()
    provider = ScriptedProvider(
        [Completion(Message.assistant(f"answer-{i}"), "end_turn") for i in range(3)]
    )
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(recent_context_messages=1),
        limits=Limits(stores.cache),
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store)],
        responder=responder,
        bot_name="bot",
        group_mentions_only=True,
        conversation_threads=ThreadSettings(continue_window_s=3600),
    )
    alice, bob = user("alice"), user("bob")

    first = message("@bot Alice first", sender=alice, mention=True, thread_id="topic")
    await app.handle(first)
    await app.handle(message("@bot Bob isolated", sender=bob, mention=True, thread_id="topic"))
    await app.handle(message("@bot Alice followup", sender=alice, mention=True, thread_id="topic"))

    alice_conversation = await store.latest_conversation(GROUP, "topic", alice.ref)
    bob_conversation = await store.latest_conversation(GROUP, "topic", bob.ref)
    assert alice_conversation is not None and bob_conversation is not None
    assert alice_conversation.id != bob_conversation.id
    alice_turns = await store.recent_conversation_turns(GROUP, alice_conversation.id, 12)
    bob_turns = await store.recent_conversation_turns(GROUP, bob_conversation.id, 12)
    assert [turn.content.get("text") for turn in alice_turns if turn.role == "user"] == [
        "Alice first",
        "Alice followup",
    ]
    assert [turn.content.get("text") for turn in bob_turns if turn.role == "user"] == [
        "Bob isolated"
    ]
    assert all(turn.conversation_id == alice_conversation.id for turn in alice_turns)


async def test_new_command_starts_fresh_and_untagged_messages_make_no_conversation():
    stores = memory_stores()
    store = ConversationStore()
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("first answer"), "end_turn"),
            Completion(Message.assistant("new answer"), "end_turn"),
        ]
    )
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(recent_context_messages=1),
        limits=Limits(stores.cache),
    )
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store)],
        responder=responder,
        bot_name="bot",
        group_mentions_only=True,
        conversation_threads=ThreadSettings(),
    )
    alice = user("alice")

    await app.handle(message("hello untagged", sender=alice, thread_id="topic"))
    assert not provider.calls
    assert not store._conversations
    await app.handle(message("/new@bot", sender=alice, thread_id="topic"))
    await app.handle(message("@bot first", sender=alice, mention=True, thread_id="topic"))
    first = await store.latest_conversation(GROUP, "topic", alice.ref)
    assert first is not None
    await app.handle(message("@bot /new what now?", sender=alice, mention=True, thread_id="topic"))
    second = await store.latest_conversation(GROUP, "topic", alice.ref)
    assert second is not None and second.id != first.id
    assert len(provider.calls) == 2
    assert any("what now?" in item.text for item in provider.calls[-1]["messages"])


async def test_old_reply_reopens_its_conversation_without_other_users_turns():
    stores = memory_stores()
    store = ConversationStore()
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("A's answer"), "end_turn"),
            Completion(Message.assistant("B's answer"), "end_turn"),
            Completion(Message.assistant("continued A"), "end_turn"),
        ]
    )
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(recent_context_messages=1),
        limits=Limits(stores.cache),
    )
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store)],
        responder=responder,
        bot_name="bot",
        group_mentions_only=True,
        conversation_threads=ThreadSettings(),
    )
    alice, bob, carol = user("alice"), user("bob"), user("carol")
    original_time = datetime(2026, 10, 31, tzinfo=UTC)
    first = replace(
        message("@bot A's question", sender=alice, mention=True, thread_id="topic"),
        sent_at=original_time,
    )
    await app.handle(first)
    a_ref = platform.sent[-1].ref
    a_conversation = await store.conversation_for_message(GROUP, a_ref.message_id)
    assert a_conversation is not None
    a_id = a_conversation[0].id
    await store.save_conversation_summary(GROUP, a_id, "A summary only", 0, generation=0)
    await app.handle(
        replace(
            message("@bot B's question", sender=bob, mention=True, thread_id="topic"),
            sent_at=original_time + timedelta(minutes=2),
        )
    )
    b_conversation = await store.latest_conversation(GROUP, "topic", bob.ref)
    assert b_conversation is not None
    await store.save_conversation_summary(
        GROUP, b_conversation.id, "B summary must not appear", 0, generation=0
    )

    replied = replace(
        message("continue this", sender=carol, thread_id="topic"),
        reply_to=MessageRef(GROUP, a_ref.message_id, "topic"),
        reply_to_bot=True,
        sent_at=original_time + timedelta(hours=3),
    )
    await app.handle(replied)

    prompt = "\n".join(item.text for item in provider.calls[-1]["messages"])
    assert "A summary only" in prompt
    assert "B summary must not appear" not in prompt
    assert "B's question" not in prompt
    assert "Replying to your earlier message" in prompt
    assert "A's answer" in prompt
    assert await store.get_conversation(GROUP, a_id) is not None
    assert carol.ref.key in (await store.get_conversation(GROUP, a_id)).participants
    assert all(
        turn.conversation_id == a_id
        for turn in await store.recent_conversation_turns(GROUP, a_id, 20)
    )


async def test_continue_window_and_automatic_compaction_are_conversation_scoped():
    store = ConversationStore()
    alice, bob = user("alice"), user("bob")
    start = datetime(2026, 10, 31, tzinfo=UTC)
    first = await store.create_conversation(GROUP, None, alice.ref, at=start)
    await store.touch_conversation(GROUP, first.id, alice.ref, at=start + timedelta(minutes=59))
    assert (
        await store.latest_conversation(GROUP, None, alice.ref, since=start + timedelta(minutes=1))
    ).id == first.id
    assert (
        await store.latest_conversation(GROUP, None, alice.ref, since=start + timedelta(minutes=61))
        is None
    )

    other = await store.create_conversation(GROUP, None, bob.ref, at=start)
    for index in range(3):
        await store.add(
            GROUP,
            None,
            "user" if index % 2 == 0 else "assistant",
            {"text": f"turn-{index}"},
            conversation_id=first.id,
        )
    provider = ScriptedProvider([Completion(Message.assistant("Alice summary"), "end_turn")])
    result = await store.compact_conversation(
        GROUP,
        first,
        provider,
        "summary rules",
        "fake",
        min_turns=1,
        keep=1,
    )

    assert result.turns_summarized == 2
    assert provider.calls[0]["tools"] == []
    assert await store.conversation_summary(GROUP, first.id) == ("Alice summary", 2)
    assert await store.conversation_summary(GROUP, other.id) is None
    assert [
        turn.content["text"] for turn in await store.recent_conversation_turns(GROUP, first.id, 10)
    ] == ["turn-2"]


async def test_manual_compact_targets_the_requesters_current_conversation():
    stores = memory_stores()
    store = ConversationStore()
    provider = ScriptedProvider([Completion(Message.assistant("manual summary"), "end_turn")])
    plugin = ConversationPlugin(store, Limits(stores.cache), provider=provider)
    alice, bob = user("alice"), user("bob")
    alice_conversation = await store.create_conversation(GROUP, "topic", alice.ref)
    bob_conversation = await store.create_conversation(GROUP, "topic", bob.ref)
    for index in range(5):
        await store.add(
            GROUP,
            "topic",
            "user" if index % 2 == 0 else "assistant",
            {"text": f"Alice turn {index}"},
            conversation_id=alice_conversation.id,
        )
    for index in range(6):
        await store.add(
            GROUP,
            "topic",
            "user" if index % 2 == 0 else "assistant",
            {"text": f"Bob turn {index}"},
            conversation_id=bob_conversation.id,
        )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=True,
        conversation_threads=ThreadSettings(),
    )

    await app.handle(message("/compact@bot", sender=alice, thread_id="topic"))

    assert len(provider.calls) == 1
    assert "Alice turn" in provider.calls[0]["messages"][0].text
    assert "Bob turn" not in provider.calls[0]["messages"][0].text
    assert await store.conversation_summary(GROUP, alice_conversation.id) == (
        "manual summary",
        1,
    )
    assert await store.conversation_summary(GROUP, bob_conversation.id) is None


async def test_reopening_old_conversation_compacts_before_answer_under_same_lock():
    stores = memory_stores()
    store = ConversationStore()
    alice = user("alice")
    old_time = datetime(2026, 10, 31, tzinfo=UTC)
    conversation = await store.create_conversation(GROUP, "topic", alice.ref, at=old_time)
    bot_ref = MessageRef(GROUP, "old-answer", "topic")
    await stores.repo.log_bot_message(
        GROUP, bot_ref.message_id, "old bot answer", thread_id="topic"
    )
    await store.map_messages(
        GROUP,
        conversation.id,
        [bot_ref.message_id],
        primary_message_id=bot_ref.message_id,
        is_bot=True,
        user=None,
    )
    for index in range(6):
        await store.add(
            GROUP,
            "topic",
            "user" if index % 2 == 0 else "assistant",
            {"text": f"old turn {index}"},
            conversation_id=conversation.id,
        )
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("old turn summary"), "end_turn"),
            Completion(Message.assistant("current answer"), "end_turn"),
        ]
    )
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(recent_context_messages=1),
        limits=Limits(stores.cache),
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store, Limits(stores.cache), provider=provider)],
        responder=responder,
        bot_name="bot",
        group_mentions_only=True,
        conversation_threads=ThreadSettings(reopen_compact_after_s=3600, reopen_keep_turns=4),
    )
    incoming = replace(
        message("please continue", sender=alice, thread_id="topic"),
        reply_to=bot_ref,
        reply_to_bot=True,
        sent_at=old_time + timedelta(hours=2),
    )

    await app.handle(incoming)

    assert len(provider.calls) == 2
    assert provider.calls[0]["tools"] == []
    assert "old turn 0" in provider.calls[0]["messages"][0].text
    assert await store.conversation_summary(GROUP, conversation.id) is not None
    assert app.platform.texts[-1] == "current answer"
    assert current_conversation.get() is None


async def test_reply_to_first_part_reopens_conversation_and_quotes_logged_answer():
    class SplitPlatform(FakePlatform):
        def sent_message_ids(self, ref):
            return ("first-part", "second-part", ref.message_id)

    stores = memory_stores()
    store = ConversationStore()
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("A long answer split into parts"), "end_turn"),
            Completion(Message.assistant("follow-up answer"), "end_turn"),
        ]
    )
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(recent_context_messages=1),
        limits=Limits(stores.cache),
    )
    platform = SplitPlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store)],
        responder=responder,
        bot_name="bot",
        group_mentions_only=True,
        conversation_threads=ThreadSettings(),
    )
    alice = user("alice")
    await app.handle(message("@bot explain", sender=alice, mention=True, thread_id="topic"))
    mapping = await store.conversation_for_message(GROUP, "first-part")
    assert mapping is not None and mapping[1] == platform.sent[-1].ref.message_id

    await app.handle(
        replace(
            message("continue", sender=alice, thread_id="topic"),
            reply_to=MessageRef(GROUP, "first-part", "topic"),
            reply_to_bot=True,
        )
    )

    prompt = "\n".join(item.text for item in provider.calls[-1]["messages"])
    assert "Replying to your earlier message" in prompt
    assert "A long answer split into parts" in prompt


async def test_reply_across_reset_starts_new_conversation_but_keeps_reply_quote():
    stores = memory_stores()
    store = ConversationStore()
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("pre reset bot reply"), "end_turn"),
            Completion(Message.assistant("fresh context answer"), "end_turn"),
        ]
    )
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(recent_context_messages=10),
        limits=Limits(stores.cache),
    )
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store)],
        responder=responder,
        bot_name="bot",
        group_mentions_only=True,
        conversation_threads=ThreadSettings(),
    )
    alice = user("alice")
    await app.handle(message("@bot pre reset user turn", sender=alice, mention=True))
    old_ref = platform.sent[-1].ref
    old_mapping = await store.conversation_for_message(GROUP, old_ref.message_id)
    assert old_mapping is not None
    old_conversation = old_mapping[0]
    await store.save_conversation_summary(
        GROUP, old_conversation.id, "pre reset secret summary", 0, generation=0
    )
    await app.handle(message("/reset@bot", sender=alice))
    await app.handle(
        replace(
            message("continue the old answer", sender=alice),
            reply_to=old_ref,
            reply_to_bot=True,
        )
    )

    new_mapping = await store.conversation_for_message(GROUP, platform.sent[-1].ref.message_id)
    assert new_mapping is not None and new_mapping[0].id != old_conversation.id
    prompt = "\n".join(item.text for item in provider.calls[-1]["messages"])
    assert "pre reset secret summary" not in prompt
    assert "pre reset user turn" not in prompt
    assert "Replying to your earlier message" in prompt
    assert "pre reset bot reply" in prompt


async def test_error_reply_is_mapped_to_the_resolved_conversation():
    class BrokenResponder:
        async def respond(self, message, text, app):
            raise RuntimeError("sensitive exception details")

        async def consider(self, message, app):
            return None

    stores = memory_stores()
    store = ConversationStore()
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store)],
        responder=BrokenResponder(),
        bot_name="bot",
        group_mentions_only=True,
        conversation_threads=ThreadSettings(),
    )

    await app.handle(message("@bot fail safely", mention=True))

    assert platform.texts == ["Sorry, something went wrong. Please try again."]
    mapping = await store.conversation_for_message(GROUP, platform.sent[-1].ref.message_id)
    assert mapping is not None


async def test_memory_retention_keeps_reset_generation_for_recent_conversation():
    store = ConversationStore()
    alice = user("alice")
    reset_at = datetime(2020, 1, 1, tzinfo=UTC)
    await store.reset(GROUP, "topic", by=alice.ref, at=reset_at)
    conversation = await store.create_conversation(GROUP, "topic", alice.ref)
    await store.save_conversation_summary(
        GROUP, conversation.id, "retained summary", 0, generation=1
    )

    await store.purge_older_than(datetime(2021, 1, 1, tzinfo=UTC))

    assert await store.latest_conversation(GROUP, "topic", alice.ref) == conversation
    assert await store.conversation_summary(GROUP, conversation.id) == (
        "retained summary",
        0,
    )


async def test_memory_summary_write_from_other_chat_does_not_replace_owner_summary():
    store = ConversationStore()
    alice = user("alice")
    other = ChatRef("fake", "other")
    conversation = await store.create_conversation(GROUP, None, alice.ref)
    await store.save_conversation_summary(GROUP, conversation.id, "owner summary", 0, generation=0)

    await store.save_conversation_summary(
        other, conversation.id, "cross chat overwrite", 0, generation=0
    )

    assert await store.conversation_summary(GROUP, conversation.id) == ("owner summary", 0)


async def test_memory_retention_cascades_expired_conversation_data():
    store = ConversationStore()
    alice = user("alice")
    old = datetime(2020, 1, 1, tzinfo=UTC)
    expired = await store.create_conversation(GROUP, "old", alice.ref, at=old)
    await store.add(
        GROUP,
        "old",
        "user",
        {"text": "recently written but attached to expired conversation"},
        "old-turn",
        conversation_id=expired.id,
    )
    await store.map_messages(
        GROUP,
        expired.id,
        ["old-map"],
        primary_message_id="old-map",
        is_bot=True,
        user=None,
    )
    await store.save_conversation_summary(GROUP, expired.id, "old summary", 0, generation=0)

    await store.purge_older_than(datetime(2021, 1, 1, tzinfo=UTC))

    assert await store.get_conversation(GROUP, expired.id) is None
    assert await store.recent_conversation_turns(GROUP, expired.id, 10) == []
    assert await store.conversation_for_message(GROUP, "old-map") is None
