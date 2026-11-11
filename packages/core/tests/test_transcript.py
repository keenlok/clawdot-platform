from dataclasses import replace
from datetime import UTC, datetime, timedelta

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.attribution import user_turn_content
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.events import Mention, MessageRef
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message, Role
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message, user
from chatcore.threads import current_conversation


def setup(*, settings=None):
    stores = memory_stores()
    provider = ScriptedProvider([Completion(Message.assistant("ok"), "end_turn")])
    responder = AgentResponder(
        provider,
        system_prompt="",
        settings=settings or AgentSettings(),
        limits=Limits(stores.cache),
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        bot_name="bot",
        responder=responder,
        group_mentions_only=True,
    )
    return stores, provider, responder, app, ConversationStore()


async def context(responder, app, store, incoming=None):
    incoming = incoming or message("@bot now", mention=True)
    return await responder._context(incoming, "now", app, store, "testtag")


async def test_transcript_interleaves_attributed_rows_and_deduplicates_turns():
    stores, _, responder, app, store = setup()
    start = datetime.now(UTC) - timedelta(minutes=2)
    alice = user("alice")
    bob = user("bob")
    first = replace(message("@bot question", mention=True, sender=alice), sent_at=start)
    await stores.repo.log_message(first)
    await store.add(GROUP, None, "user", {"text": first.text}, first.message_id)
    await stores.repo.log_bot_message(GROUP, "bot-1", "answer", thread_id=None)
    await store.add(GROUP, None, "assistant", {"text": "answer"})
    bot_row = await stores.repo.get_message(GROUP, "bot-1")
    assert bot_row is not None
    bob_row = replace(
        message("ambient from Bob", sender=bob), sent_at=bot_row.sent_at + timedelta(seconds=1)
    )
    await stores.repo.log_message(bob_row)

    _, turns, *_ = await context(responder, app, store)
    transcript = next(item for item in turns if "Recent conversation here" in item.text)
    payload = transcript.text.split(">", 1)[1].rsplit("</untrusted-", 1)[0]
    assert payload.index('"user_key":"fake:alice"') < payload.index('"user_key":"fake:bot"')
    assert payload.index('"user_key":"fake:bot"') < payload.index('"user_key":"fake:bob"')
    assert payload.count("answer") == 1
    assert payload.count("@bot question") == 1
    assert transcript.role is Role.USER
    assert len([item for item in turns if item.role is Role.ASSISTANT]) == 0


async def test_old_turns_outside_log_window_are_retained_before_log_rows():
    stores, _, responder, app, store = setup(settings=AgentSettings(recent_context_messages=2))
    old = await store.add(GROUP, None, "user", {"text": "older turn"}, "old-id")
    for idx in range(3):
        await stores.repo.log_message(message(f"row-{idx}", sender=user(f"u{idx}")))
    _, turns, *_ = await context(responder, app, store)
    transcript = next(item.text for item in turns if "Recent conversation here" in item.text)
    assert transcript.index("older turn") < transcript.index("row-1")
    assert '"display_name":"unknown speaker"' in transcript
    assert old.created_at.isoformat() in transcript


async def test_reset_filters_old_rows_and_turns_from_transcript():
    stores, _, responder, app, store = setup()
    old_time = datetime.now(UTC) - timedelta(minutes=1)
    old_row = replace(message("pre-reset row"), sent_at=old_time)
    await stores.repo.log_message(old_row)
    old_turn = await store.add(GROUP, None, "user", {"text": "pre-reset turn"}, "old-turn")
    await store.reset(GROUP, None, by=user().ref, at=datetime.now(UTC))
    await stores.repo.log_message(message("post-reset row", sender=user("bob")))
    _, turns, *_ = await context(responder, app, store)
    all_text = "\n".join(item.text for item in turns)
    assert "pre-reset row" not in all_text
    assert "pre-reset turn" not in all_text
    assert old_turn.created_at.isoformat() not in all_text


async def test_transcript_caps_human_and_bot_entries_and_keeps_newest_lines():
    stores, _, responder, app, store = setup()
    await stores.repo.log_message(message("x" * 10_000, sender=user("long")))
    await stores.repo.log_bot_message(GROUP, "long-bot", "y" * 10_000, thread_id=None)
    _, turns, *_ = await context(responder, app, store)
    transcript = next(item.text for item in turns if "Recent conversation here" in item.text)
    payload = transcript.split(">", 1)[1].rsplit("</untrusted-", 1)[0]
    assert "x" * 1500 + "…[truncated]" in payload
    assert "y" * 1500 + "…[truncated]" in payload
    assert "(this bot)" in payload and "(you)" not in payload

    stores, _, responder, app, store = setup(settings=AgentSettings())
    for idx in range(8):
        await stores.repo.log_message(
            message(f"latest-{idx}-" + str(idx) * 1490, sender=user(f"u{idx}"))
        )
    _, turns, *_ = await context(responder, app, store)
    transcript = next(item.text for item in turns if "Recent conversation here" in item.text)
    payload = transcript.split(">", 1)[1].rsplit("</untrusted-", 1)[0]
    assert "latest-7-" in payload and "latest-0-" not in payload
    assert len(payload) <= 6_000 + 80


async def test_transcript_cap_keeps_newest_contiguous_suffix_without_gaps():
    stores, _, responder, app, store = setup(
        settings=AgentSettings(ambient_total_chars=270, ambient_message_chars=1000)
    )
    base = datetime.now(UTC) - timedelta(minutes=1)
    for index, text in enumerate(("old short", "middle " + "m" * 220, "new short")):
        row = replace(
            message(text, sender=user(f"u{index}")),
            sent_at=base + timedelta(seconds=index),
        )
        await stores.repo.log_message(row)
    _, turns, *_ = await context(responder, app, store)
    transcript = next(item.text for item in turns if "Recent conversation here" in item.text)
    assert "new short" in transcript
    assert "middle " not in transcript
    assert "old short" not in transcript


async def test_rows_from_other_threads_never_enter_topic_transcript():
    stores, _, responder, app, store = setup(settings=AgentSettings(recent_context_messages=20))
    await stores.repo.log_message(message("topic row", thread_id="topic"))
    await stores.repo.log_message(message("other topic row", thread_id="other"))
    await stores.repo.log_message(message("no topic row"))
    topic = message("@bot now", mention=True, thread_id="topic")
    _, turns, *_ = await context(responder, app, store, topic)
    text = "\n".join(item.text for item in turns)
    assert "topic row" in text
    assert "other topic row" not in text
    assert "no topic row" not in text


async def test_unthreaded_transcript_filters_topic_rows_from_all_thread_log_query():
    stores, _, responder, app, store = setup(settings=AgentSettings(recent_context_messages=20))
    await stores.repo.log_message(message("unthreaded row", thread_id=None))
    await stores.repo.log_message(message("topic-a row", thread_id="topic-a"))
    await stores.repo.log_message(message("topic-b row", thread_id="topic-b"))
    unthreaded = message("@bot now", mention=True, thread_id=None)
    _, turns, *_ = await context(responder, app, store, unthreaded)
    text = "\n".join(item.text for item in turns)
    assert "unthreaded row" in text
    assert "topic-a row" not in text
    assert "topic-b row" not in text


async def test_replied_to_old_bot_message_is_wrapped_even_after_reset_and_log_window():
    stores = memory_stores()
    provider = ScriptedProvider([Completion(Message.assistant("ok"), "end_turn")])
    store = ConversationStore()
    responder = AgentResponder(
        provider,
        system_prompt="",
        settings=AgentSettings(recent_context_messages=1, ambient_message_chars=8),
        limits=Limits(stores.cache),
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store)],
        bot_name="bot",
        responder=responder,
        group_mentions_only=True,
    )
    await stores.repo.log_bot_message(
        GROUP, "target", "old bot words plus hidden remainder", thread_id=None
    )
    target = await stores.repo.get_message(GROUP, "target")
    assert target is not None
    for idx in range(4):
        await stores.repo.log_message(message(f"later-{idx}", sender=user(f"u{idx}")))
    await app.handle(message("/reset@bot"))
    incoming = replace(
        message("@bot followup", mention=True, reply_to_bot=True),
        reply_to=MessageRef(GROUP, "target"),
    )
    await app.handle(incoming)
    assert len(provider.calls) == 1
    reply_block = next(
        part.text
        for part in provider.calls[0]["messages"]
        if "Replying to your earlier message" in part.text
    )
    assert "old bot …[truncated]" in reply_block
    assert "plus hidden remainder" not in reply_block
    assert reply_block.startswith("<untrusted-") and reply_block.endswith(">")


async def test_replied_to_bot_outside_log_and_turn_windows_reaches_provider_wrapped():
    stores, provider, _, app, _ = setup(
        settings=AgentSettings(recent_context_messages=1, recent_turns=1)
    )
    await stores.repo.log_bot_message(GROUP, "target", "older bot reply", thread_id=None)
    for idx in range(4):
        await stores.repo.log_message(message(f"later-{idx}", sender=user(f"u{idx}")))
    incoming = replace(
        message("follow up", reply_to_bot=True), reply_to=MessageRef(GROUP, "target")
    )
    await app.handle(incoming)
    assert len(provider.calls) == 1
    prompt = "\n".join(part.text for part in provider.calls[0]["messages"])
    assert "older bot reply" in prompt
    assert "<untrusted-" in prompt and "Replying to your earlier message" in prompt


async def test_mention_only_reply_to_bot_with_empty_turn_history_includes_target_text():
    stores, provider, _, app, _ = setup()
    await stores.repo.log_bot_message(GROUP, "bot-msg", "Please send the details", thread_id=None)
    incoming = replace(message("details", reply_to_bot=True), reply_to=MessageRef(GROUP, "bot-msg"))
    await app.handle(incoming)
    assert len(provider.calls) == 1
    prompt = "\n".join(part.text for part in provider.calls[0]["messages"])
    assert "Please send the details" in prompt
    assert "Replying to your earlier message" in prompt


async def test_prompt_records_keep_historical_speakers_mentions_and_reply_edges():
    stores, _, responder, app, store = setup(settings=AgentSettings(recent_context_messages=1))
    alice = replace(user("alice"), display_name="Alex", username="alice_old")
    bob = replace(user("bob"), display_name="Alex", username="bob_old")
    carol = replace(user("carol"), display_name="Alex", username="carol_old")
    origin_text = "I call Bob Captain. Alex"
    origin = replace(
        message(origin_text, sender=alice),
        mentions=(
            Mention(
                origin_text.index("Alex"),
                origin_text.index("Alex") + 4,
                "text_mention",
                "Alex",
                bob.ref,
                bob.username,
                bob.display_name,
            ),
        ),
    )
    await stores.repo.log_message(origin)
    await store.add(
        GROUP,
        None,
        "user",
        user_turn_content(origin, "I call Bob Captain. Alex"),
        origin.message_id,
    )
    bridge_text = "Alex heard that"
    bridge = replace(
        message(bridge_text, sender=bob),
        reply_to=origin.ref,
        mentions=(Mention(0, 4, "text_mention", "Alex", alice.ref, alice.username, "Alex"),),
    )
    await stores.repo.log_message(bridge)
    raw_current = "@bot who calls whom Captain, Alex?"
    current = replace(
        message(raw_current, sender=carol, mention=True),
        reply_to=bridge.ref,
        mentions=(
            Mention(
                raw_current.index("Alex"),
                raw_current.index("Alex") + 4,
                "text_mention",
                "Alex",
                bob.ref,
                bob.username,
                "Alex",
            ),
        ),
    )

    _, turns, *_ = await responder._context(
        current, "who calls whom Captain, Alex?", app, store, "testtag"
    )
    prompt = "\n".join(item.text for item in turns)

    assert '"user_key":"fake:alice"' in prompt and '"username":"alice_old"' in prompt
    assert '"user_key":"fake:bob"' in prompt and '"username":"bob_old"' in prompt
    assert '"user_key":"fake:carol"' in prompt and '"username":"carol_old"' in prompt
    assert '"target_user_key":"fake:bob"' in prompt
    assert '"target_user_key":"fake:alice"' in prompt
    assert "I call Bob Captain" in prompt and "Alex heard that" in prompt
    assert f'"reply_to_id":"{bridge.message_id}"' in prompt
    assert f'"message_id":"{current.message_id}"' in prompt
    assert '"role":"user"' in prompt
    final = turns[-1].text
    assert final.startswith("<untrusted-testtag>Message from Alex (@carol_old, fake:carol)")
    assert final.split("</untrusted-testtag>", 1)[1] == "\nwho calls whom Captain, Alex?"
    assert final.count("<untrusted-") == 1
    assert not any(item.text.startswith("Alex: who calls") for item in turns)


async def test_current_sender_metadata_survives_many_mentions():
    _, _, responder, app, store = setup()
    raw_text = "@bot " + " ".join(["@friend"] * 16) + " Who said this?"
    mentions = []
    offset = 0
    while (start := raw_text.find("@friend", offset)) >= 0:
        mentions.append(
            Mention(start, start + len("@friend"), "mention", "@friend", username="friend")
        )
        offset = start + len("@friend")
    current = replace(
        message(raw_text, sender=user("alice"), mention=True),
        mentions=tuple(mentions),
        reply_to=MessageRef(GROUP, "parent-message"),
    )

    _, turns, *_ = await responder._context(current, "Who said this?", app, store, "testtag")

    current_metadata = next(part.text for part in turns if current.message_id in part.text)
    assert '"role":"user"' in current_metadata
    assert f'"message_id":"{current.message_id}"' in current_metadata
    assert '"user_key":"fake:alice"' in current_metadata
    assert '"reply_to_id":"parent-message"' in current_metadata
    final = turns[-1].text
    assert final.split("</untrusted-testtag>", 1)[1] == "\nWho said this?"
    assert final.count("<untrusted-") == 1
    assert "replying to parent-message" in turns[-1].text


async def test_reply_to_another_members_bot_answer_has_provenance_and_handover():
    stores, _, responder, app, store = setup()
    member_a = replace(user("alice"), display_name="Member A")
    member_b = replace(user("bob"), display_name="Member B")
    original = message("Please split it", sender=member_a)
    await stores.repo.log_message(original)
    await stores.repo.upsert_member(GROUP, member_a)
    await stores.repo.upsert_member(GROUP, member_b)
    await stores.repo.log_bot_message(
        GROUP, "bot-answer", "Member A paid this", thread_id=None, reply_to_id=original.message_id
    )
    conversation = await store.create_conversation(GROUP, None, member_a.ref)
    await stores.repo.log_message(message("Member B present", sender=member_b))
    current = replace(
        message("please fix the split", sender=member_b, reply_to_bot=True),
        reply_to=MessageRef(GROUP, "bot-answer"),
    )
    token = current_conversation.set(conversation)
    try:
        _, turns, *_ = await responder._context(current, current.text, app, store, "testtag")
    finally:
        current_conversation.reset(token)

    prompt = "\n".join(item.text for item in turns)
    assert "which answered Member A (fake:alice)" in prompt
    assert '"you" in it means them, not the current speaker.' in prompt
    assert "Member B is joining a conversation started by Member A" in prompt
    assert "The summary and earlier turns were written for them." in prompt


async def test_chat_compaction_keeps_historical_speakers_and_marks_legacy_unknown():
    store = ConversationStore()
    alice = replace(user("alice"), display_name="Alex", username="alice_old")
    bob = replace(user("bob"), display_name="Alex", username="bob_old")
    first = message("I call Bob Captain", sender=alice)
    second = replace(message("Bob calls Alice Ace", sender=bob), reply_to=first.ref)
    await store.add(
        GROUP, None, "user", user_turn_content(first, "I call Bob Captain"), first.message_id
    )
    await store.add(GROUP, None, "assistant", {"text": "acknowledged"})
    await store.add(
        GROUP, None, "user", user_turn_content(second, "Bob calls Alice Ace"), second.message_id
    )
    await store.add(GROUP, None, "user", {"text": "legacy turn"}, "legacy-id")
    provider = ScriptedProvider([Completion(Message.assistant("summary"), "end_turn")])

    await store.compact(GROUP, None, provider, "summary rules", "fake-model", force=True, keep=0)

    transcript = provider.calls[0]["messages"][0].text
    assert '"user_key":"fake:alice"' in transcript and '"username":"alice_old"' in transcript
    assert '"user_key":"fake:bob"' in transcript and '"username":"bob_old"' in transcript
    assert '"reply_to_id":"' + first.message_id + '"' in transcript
    assert "I call Bob Captain" in transcript and "Bob calls Alice Ace" in transcript
    assert '"display_name":"unknown speaker"' in transcript
    assert "You write internal notes about a chat" in provider.calls[0]["system"]


async def test_conversation_compaction_summary_reopens_with_attribution_guidance():
    stores = memory_stores()
    store = ConversationStore()
    alice = replace(user("alice"), display_name="Alex", username="alice_old")
    bob = replace(user("bob"), display_name="Alex", username="bob_old")
    conversation = await store.create_conversation(GROUP, "topic", alice.ref)
    first = message("I call Bob Captain", sender=alice, thread_id="topic")
    second = replace(
        message("Bob calls Alice Ace", sender=bob, thread_id="topic"), reply_to=first.ref
    )
    await store.add(
        GROUP,
        "topic",
        "user",
        user_turn_content(first, "I call Bob Captain"),
        first.message_id,
        conversation_id=conversation.id,
    )
    await store.add(
        GROUP,
        "topic",
        "user",
        user_turn_content(second, "Bob calls Alice Ace"),
        second.message_id,
        conversation_id=conversation.id,
    )
    provider = ScriptedProvider(
        [Completion(Message.assistant("Alice calls Bob Captain"), "end_turn")]
    )

    await store.compact_conversation(
        GROUP,
        conversation,
        provider,
        "summary rules",
        "fake-model",
        force=True,
        keep=0,
        min_turns=1,
    )

    transcript = provider.calls[0]["messages"][0].text
    assert '"user_key":"fake:alice"' in transcript and '"username":"alice_old"' in transcript
    assert '"user_key":"fake:bob"' in transcript and '"username":"bob_old"' in transcript
    assert "You write internal notes about a chat" in provider.calls[0]["system"]

    token = current_conversation.set(conversation)
    try:
        responder = AgentResponder(
            ScriptedProvider([]),
            system_prompt="",
            settings=AgentSettings(),
            limits=Limits(stores.cache),
        )
        app = BotApp(
            FakePlatform(),
            stores=stores,
            access=AccessPolicy([GROUP.key], []),
            plugins=[ConversationPlugin(store)],
            bot_name="bot",
            responder=responder,
            group_mentions_only=True,
        )
        _, turns, *_ = await responder._context(
            message("@bot continue", sender=alice, mention=True, thread_id="topic"),
            "continue",
            app,
            store,
            "testtag",
        )
    finally:
        current_conversation.reset(token)
    assert any("Alice calls Bob Captain" in item.text for item in turns)
