from dataclasses import replace
from datetime import UTC, datetime, timedelta

from chatcore.events import ChatRef, Mention
from chatcore.pending import PendingAction
from chatcore.store.base import ChatSettings
from chatcore.store.memory import MemoryCoreRepo
from chatcore.testing import GROUP, message, user


async def test_memory_purges_are_chat_scoped_and_retain_member_on_forgetme():
    repo = MemoryCoreRepo()
    other = ChatRef("fake", "other")
    alice, bob = user("alice"), user("bob")
    for chat, sender, text in ((GROUP, alice, "a1"), (GROUP, bob, "b1"), (other, alice, "a2")):
        await repo.log_message(message(text, sender=sender, chat=chat))
        await repo.upsert_member(chat, sender)
    action_a = PendingAction.create(
        chat=GROUP, requester=alice.ref, plugin="p", kind="x", payload={}, summary="s"
    )
    action_b = PendingAction.create(
        chat=other, requester=alice.ref, plugin="p", kind="x", payload={}, summary="s"
    )
    await repo.save_pending(action_a)
    await repo.save_pending(action_b)

    await repo.purge_user(GROUP, alice.ref)
    assert [row.text for row in await repo.recent_messages(GROUP, limit=10)] == ["b1"]
    assert [row.text for row in await repo.recent_messages(other, limit=10)] == ["a2"]
    assert await repo.get_pending(GROUP, action_a.id) is None
    assert await repo.get_pending(other, action_b.id) is not None
    assert [u.ref for u in await repo.find_members(GROUP)] == [alice.ref, bob.ref]


async def test_memory_purge_chat_and_retention_leave_other_chat_intact():
    repo = MemoryCoreRepo()
    other = ChatRef("fake", "other")
    alice = user("alice")
    await repo.log_message(message("old", sender=alice, chat=GROUP))
    await repo.log_message(message("keep", sender=alice, chat=other))
    await repo.purge_chat(GROUP)
    assert await repo.recent_messages(GROUP, limit=10) == []
    assert [row.text for row in await repo.recent_messages(other, limit=10)] == ["keep"]
    cutoff = datetime.now(UTC) + timedelta(days=1)
    assert await repo.purge_older_than(cutoff, now=datetime.now(UTC)) == 1
    assert await repo.recent_messages(other, limit=10) == []


async def test_memory_forgetme_clears_surviving_normalized_mention_snapshots():
    repo = MemoryCoreRepo()
    other = ChatRef("fake", "other")
    alice, bob = user("alice"), user("bob")
    own = message("my message", sender=alice)
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

    removed = await repo.purge_user(GROUP, alice.ref)

    assert removed == 1
    assert await repo.get_message(GROUP, own.message_id) is None
    assert (await repo.get_message(GROUP, by_id.message_id)).mentions == ()
    assert (await repo.get_message(GROUP, by_handle.message_id)).mentions == ()
    assert (await repo.get_message(other, other_copy.message_id)).mentions == other_copy.mentions
    assert alice.ref in [member.ref for member in await repo.find_members(GROUP)]


async def test_purging_chat_b_preserves_chat_a_user_data():
    repo = MemoryCoreRepo()
    chat_a, chat_b = ChatRef("fake", "a"), ChatRef("fake", "b")
    alice = user("alice")
    await repo.log_message(message("A stays", sender=alice, chat=chat_a))
    await repo.log_message(message("B removed", sender=alice, chat=chat_b))
    await repo.purge_user(chat_b, alice.ref)
    assert [row.text for row in await repo.recent_messages(chat_a, limit=10)] == ["A stays"]
    await repo.log_message(message("B removed again", sender=alice, chat=chat_b))
    await repo.purge_chat(chat_b)
    assert [row.text for row in await repo.recent_messages(chat_a, limit=10)] == ["A stays"]


async def test_memory_retention_removes_old_messages_and_used_or_expired_actions_only():
    repo = MemoryCoreRepo()
    alice = user("alice")
    now = datetime.now(UTC)
    old, recent = message("old", sender=alice), message("recent", sender=alice)
    await repo.log_message(replace(old, sent_at=now - timedelta(days=400)))
    await repo.log_message(replace(recent, sent_at=now))
    await repo.upsert_member(GROUP, alice)
    await repo.save_chat_settings(ChatSettings(GROUP, persona="shared"), updated_by=alice.ref)

    def action(action_id):
        return PendingAction.create(
            chat=GROUP,
            requester=alice.ref,
            plugin="p",
            kind="x",
            payload={},
            summary="s",
            now=now,
        )

    used = replace(action("used"), id="used", used_at=now)
    expired = replace(action("expired"), id="expired", expires_at=now - timedelta(seconds=1))
    live = replace(action("live"), id="live", expires_at=now + timedelta(days=1))
    for pending in (used, expired, live):
        await repo.save_pending(pending)

    assert await repo.purge_older_than(now - timedelta(days=365), now=now) == 3
    assert [row.text for row in await repo.recent_messages(GROUP, limit=10)] == ["recent"]
    assert await repo.get_pending(GROUP, "used") is None
    assert await repo.get_pending(GROUP, "expired") is None
    assert await repo.get_pending(GROUP, "live") is not None
    assert await repo.find_members(GROUP) == [alice]
    assert (await repo.get_chat_settings(GROUP)).persona == "shared"
