from dataclasses import replace
from datetime import UTC, datetime, timedelta

from chatcore.attribution import user_turn_content
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.events import ChatRef, Mention
from chatcore.testing import GROUP, memory_stores, message, user


async def test_memory_conversation_purge_user_uses_core_message_ids_and_chat_scope():
    stores = memory_stores()
    alice, bob = user("alice"), user("bob")
    chat_b = ChatRef("fake", "b")
    alice_a, bob_a, alice_b = (
        message("a", sender=alice),
        message("b", sender=bob),
        message("other", sender=alice, chat=chat_b),
    )
    for incoming in (alice_a, bob_a, alice_b):
        await stores.repo.log_message(incoming)
    store = ConversationStore()
    alice_turn = await store.add(GROUP, None, "user", {"text": "a"}, alice_a.message_id)
    await store.add(GROUP, None, "assistant", {"text": "quotes alice"}, alice_a.message_id)
    await store.add(GROUP, None, "user", {"text": "b"}, bob_a.message_id)
    await store.add(chat_b, None, "user", {"text": "other"}, alice_b.message_id)
    await store.save_summary(GROUP, None, "Alice summary", alice_turn.id)
    await store.record_usage(GROUP, alice.ref, "fake", "fake", 2, 3)
    plugin = ConversationPlugin(store)

    await plugin.purge_user(GROUP, alice.ref, stores)
    assert [turn.content.get("text") for turn in await store.recent(GROUP, None)] == ["b"]
    assert await store.summary(GROUP, None) is None
    assert len(await store.recent(chat_b, None)) == 1
    assert store._usage == []


async def test_memory_retention_deletes_old_pre_reset_turn_and_keeps_newer_turn():
    store = ConversationStore()
    old = datetime.now(UTC) - timedelta(days=400)
    cutoff = datetime.now(UTC) - timedelta(days=365)
    old_turn = await store.add(GROUP, None, "user", {"text": "old"})
    store._turns = [replace(old_turn, created_at=old)]
    await store.reset(GROUP, None, by=user().ref, at=old + timedelta(days=1))
    recent_turn = await store.add(GROUP, None, "user", {"text": "recent"})
    recent_time = cutoff + timedelta(seconds=1)
    store._turns = [
        replace(turn, created_at=recent_time if turn.id == recent_turn.id else turn.created_at)
        for turn in store._turns
    ]
    assert len(store._turns) == 2
    assert [turn.id for turn in await store.recent(GROUP, None)] == [recent_turn.id]

    await store.purge_older_than(cutoff)
    assert [turn.id for turn in store._turns] == [recent_turn.id]
    assert [turn.id for turn in await store.recent(GROUP, None)] == [recent_turn.id]


async def test_forgetme_deletes_expired_source_turns_and_clears_surviving_mentions():
    stores = memory_stores()
    alice, bob = user("alice"), user("bob")
    other = ChatRef("fake", "other")
    own_row = message("own raw message", sender=alice)
    bob_row = replace(
        message("Alice still said this", sender=bob),
        mentions=(Mention(0, 5, "text_mention", "Alice", alice.ref, "alice_old", "Alice"),),
    )
    other_row = replace(
        message("Alice in other chat", sender=bob, chat=other),
        mentions=(Mention(0, 5, "text_mention", "Alice", alice.ref, "alice_old", "Alice"),),
    )
    for incoming in (own_row, bob_row, other_row):
        await stores.repo.log_message(incoming)
    await stores.repo.upsert_member(GROUP, alice)
    store = ConversationStore()
    conversation = await store.create_conversation(GROUP, None, alice.ref)
    expired_source = message("expired source", sender=alice)
    expired_content = user_turn_content(expired_source, "expired source")
    await store.add(
        GROUP,
        None,
        "user",
        expired_content,
        "expired-source-id",
        conversation_id=conversation.id,
    )
    await store.add(
        GROUP,
        None,
        "assistant",
        {"text": "assistant linked to expired source"},
        "expired-source-id",
        conversation_id=conversation.id,
    )
    await store.add(
        GROUP,
        None,
        "tool",
        {"text": "tool linked to expired source"},
        "expired-source-id",
        conversation_id=conversation.id,
    )
    bob_turn = await store.add(
        GROUP,
        None,
        "user",
        user_turn_content(bob_row, "Alice still said this"),
        bob_row.message_id,
        conversation_id=conversation.id,
    )
    other_turn = await store.add(
        other,
        None,
        "user",
        user_turn_content(other_row, "Alice in other chat"),
        other_row.message_id,
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

    await plugin.purge_user(GROUP, alice.ref, stores)
    await stores.repo.purge_user(GROUP, alice.ref)

    remaining = await store.recent(GROUP, None)
    assert [turn.id for turn in remaining] == [bob_turn.id]
    assert remaining[0].content["text"] == "Alice still said this"
    assert "mentions" not in remaining[0].content
    assert "speaker" in remaining[0].content
    assert await store.summary(GROUP, None) is None
    assert await store.conversation_summary(GROUP, conversation.id) is None
    remaining_other = (await store.recent(other, None))[0]
    assert remaining_other.id == other_turn.id
    assert (
        remaining_other.content["mentions"]
        == user_turn_content(other_row, "Alice in other chat")["mentions"]
    )
    assert (await stores.repo.get_message(GROUP, own_row.message_id)) is None
    assert (await stores.repo.get_message(GROUP, bob_row.message_id)).mentions == ()
    other_after = await stores.repo.get_message(other, other_row.message_id)
    assert other_after.mentions == other_row.mentions
    assert alice.ref in [member.ref for member in await stores.repo.find_members(GROUP)]
