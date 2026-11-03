from dataclasses import replace
from datetime import UTC, datetime, timedelta

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationStore
from chatcore.events import ChatRef, MessageRef
from chatcore.limits import Limits
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message, user
from chatcore.tools.registry import ToolContext, TurnState


def test_human_provenance_field_preserves_existing_turn_state_positional_arguments():
    assert TurnState(set(), True).read_untrusted is True


def setup(*, recent_context_messages=20):
    stores = memory_stores()
    platform = FakePlatform()
    responder = AgentResponder(
        ScriptedProvider([]),
        system_prompt="",
        settings=AgentSettings(recent_context_messages=recent_context_messages),
        limits=Limits(stores.cache),
    )
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        bot_name="bot",
        responder=responder,
    )
    return stores, platform, responder, app, ConversationStore()


async def urls_for(responder, app, store, incoming):
    context = await responder._context(incoming, incoming.text, app, store, "testtag")
    return set(context[2])


async def test_authenticated_human_urls_exclude_ambient_and_bot_links():
    stores, platform, responder, app, store = setup()
    origin = message("https://docs.google.com/spreadsheets/d/human/edit")
    await stores.repo.log_message(origin)
    await stores.repo.log_message(message("https://docs.google.com/document/d/ambient/edit"))
    bot_ref = await app.send(
        GROUP, "https://docs.google.com/document/d/bot/edit", reply_to=origin.ref
    )
    incoming = replace(message("read https://example.com/current"), reply_to=bot_ref)
    ctx = ToolContext(GROUP, incoming.user, False, platform, stores)
    await responder._context(incoming, incoming.text, app, store, "tag", tool_ctx=ctx)
    assert ctx.turn.human_urls == frozenset(
        {"https://example.com/current", "https://docs.google.com/spreadsheets/d/human/edit"}
    )
    ctx.turn.seen_urls.add("https://docs.google.com/document/d/search/edit")
    assert all("search" not in url and "ambient" not in url for url in ctx.turn.human_urls)


async def test_reply_chain_keeps_human_link_when_origin_is_outside_ambient_window():
    stores, _, responder, app, store = setup(recent_context_messages=1)
    origin = message("https://share.google/abc123", sender=user("alice"))
    bridge = replace(message("@bot take a look", sender=user("bob")), reply_to=origin.ref)
    await stores.repo.log_message(origin)
    await stores.repo.log_message(bridge)
    bot_ref = await app.send(GROUP, "I will check", reply_to=bridge.ref)
    incoming = replace(message("try again", sender=user("dana")), reply_to=bot_ref)
    await stores.repo.log_message(message("filler", sender=user("erin")))

    assert await urls_for(responder, app, store, incoming) == {"https://share.google/abc123"}
    bot_row = await stores.repo.get_message(GROUP, bot_ref.message_id)
    assert bot_row is not None and bot_row.reply_to_id == bridge.message_id


async def test_reply_chain_stops_at_reset_but_direct_human_reply_still_seeds_link():
    stores, _, responder, app, store = setup(recent_context_messages=1)
    origin = replace(
        message("https://share.google/pre-reset", sender=user("alice")),
        sent_at=datetime.now(UTC) - timedelta(minutes=3),
    )
    bridge = replace(
        message("@bot take a look", sender=user("bob")),
        reply_to=origin.ref,
        sent_at=origin.sent_at + timedelta(seconds=1),
    )
    await stores.repo.log_message(origin)
    await stores.repo.log_message(bridge)
    bot_ref = await app.send(GROUP, "I will check", reply_to=bridge.ref)
    bot_row = await stores.repo.get_message(GROUP, bot_ref.message_id)
    assert bot_row is not None
    reset_at = bot_row.sent_at + timedelta(seconds=1)
    await store.reset(GROUP, None, by=user().ref, at=reset_at)
    await stores.repo.log_message(message("filler", sender=user("erin")))

    followup = replace(
        message("try again", sender=user("dana")),
        reply_to=bot_ref,
        sent_at=reset_at + timedelta(seconds=1),
    )
    direct_reply = replace(
        message("what about this?", sender=user("dana")),
        reply_to=origin.ref,
        sent_at=reset_at + timedelta(seconds=2),
    )

    assert await urls_for(responder, app, store, followup) == set()
    assert await urls_for(responder, app, store, direct_reply) == {"https://share.google/pre-reset"}


async def test_bot_message_urls_are_never_seeded_from_reply_or_ambient_history():
    stores, _, responder, app, store = setup(recent_context_messages=20)
    bot_ref = await app.send(GROUP, "https://example.invalid/bot-only")
    incoming = replace(message("try again"), reply_to=bot_ref)

    assert await urls_for(responder, app, store, incoming) == set()


async def test_reply_chain_stops_at_cross_thread_row_and_does_not_follow_ancestor():
    stores, _, responder, app, store = setup(recent_context_messages=1)
    ancestor = message("https://example.invalid/ancestor", thread_id="topic")
    middle = replace(message("cross-thread", thread_id="other"), reply_to=ancestor.ref)
    target = replace(message("target", thread_id="topic"), reply_to=middle.ref)
    await stores.repo.log_message(ancestor)
    await stores.repo.log_message(middle)
    await stores.repo.log_message(target)
    await stores.repo.log_message(message("filler", thread_id="topic"))
    incoming = replace(message("question", thread_id="topic"), reply_to=target.ref)

    assert await urls_for(responder, app, store, incoming) == set()


async def test_reply_chain_lookup_is_scoped_to_chat():
    stores, _, responder, app, store = setup(recent_context_messages=1)
    other_chat = ChatRef(GROUP.platform, "other")
    await stores.repo.log_message(
        replace(
            message("https://example.invalid/other-chat", chat=other_chat),
            message_id="shared-id",
        )
    )
    target = replace(message("target"), reply_to=MessageRef(GROUP, "shared-id"))
    await stores.repo.log_message(target)
    await stores.repo.log_message(message("filler"))
    incoming = replace(message("question"), reply_to=target.ref)

    assert await urls_for(responder, app, store, incoming) == set()


async def test_reply_chain_hop_limit_and_cycle_guard():
    stores, _, responder, app, store = setup(recent_context_messages=1)
    rows = [message("https://example.invalid/oldest")]
    for index in range(4):
        rows.append(replace(message(f"row {index}"), reply_to=rows[-1].ref))
    for row in rows:
        await stores.repo.log_message(row)
    await stores.repo.log_message(message("filler"))
    incoming = replace(message("question"), reply_to=rows[-1].ref)
    assert await urls_for(responder, app, store, incoming) == set()

    cycle_stores, _, responder, app, store = setup(recent_context_messages=1)
    first = replace(
        message("cycle one"), message_id="cycle-one", reply_to=MessageRef(GROUP, "cycle-two")
    )
    second = replace(
        message("https://example.invalid/cycle"), message_id="cycle-two", reply_to=first.ref
    )
    await cycle_stores.repo.log_message(first)
    await cycle_stores.repo.log_message(second)
    await cycle_stores.repo.log_message(message("filler"))
    incoming = replace(message("question"), reply_to=second.ref)

    assert await urls_for(responder, app, store, incoming) == {"https://example.invalid/cycle"}


async def test_recent_human_links_respect_thread_and_reset_point():
    stores, _, responder, app, store = setup(recent_context_messages=20)
    old_time = datetime.now(UTC) - timedelta(minutes=2)
    old = replace(message("https://example.invalid/before-reset"), sent_at=old_time)
    await stores.repo.log_message(old)
    await store.reset(GROUP, None, by=user().ref, at=datetime.now(UTC) - timedelta(minutes=1))
    await stores.repo.log_message(message("https://example.invalid/after-reset"))
    await stores.repo.log_message(
        message("https://example.invalid/other-thread", thread_id="elsewhere")
    )
    incoming = message("question")

    assert await urls_for(responder, app, store, incoming) == {
        "https://example.invalid/after-reset"
    }


async def test_log_bot_message_accepts_reply_target():
    stores, _, _, _, _ = setup()
    await stores.repo.log_bot_message(
        GROUP, "bot-id", "answer", thread_id=None, reply_to_id="human-id"
    )

    row = await stores.repo.get_message(GROUP, "bot-id")
    assert row is not None and row.reply_to_id == "human-id"
