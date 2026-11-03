import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from chatcore.access import AccessPolicy
from chatcore.administration.store import AdminChange, MemoryAdministrationStore
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.events import ChatRef, MessageRef, UserRef
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message, Usage
from chatcore.routing import Route, addressed_command, decide
from chatcore.store.base import ChatSettings, StoredMessage
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message, user


async def test_reset_keeps_turn_rows_but_hides_them_until_new_turns():
    store = ConversationStore()
    actor = UserRef("fake", "alice")
    old = await store.add(GROUP, None, "user", {"text": "before"}, "old")

    point = await store.reset(GROUP, None, by=actor)

    assert point.after_turn_id == old.id
    assert point.generation == 1
    assert await store.recent(GROUP, None) == []
    assert len(store._turns) == 1
    new = await store.add(GROUP, None, "user", {"text": "after"}, "new")
    assert [turn.id for turn in await store.recent(GROUP, None)] == [new.id]
    second_point = await store.reset(GROUP, None, by=actor)
    assert second_point.generation == 2


async def test_reset_point_is_scoped_and_filters_stale_summary():
    store = ConversationStore()
    topic = await store.add(GROUP, "topic-a", "user", {"text": "old"})
    other_topic = await store.add(GROUP, "topic-b", "user", {"text": "other"})
    other_chat = ChatRef("fake", "elsewhere")
    await store.add(other_chat, None, "user", {"text": "private"})
    await store.save_summary(GROUP, "topic-a", "stale summary", topic.id)

    await store.reset(GROUP, "topic-a", by=UserRef("fake", "alice"))

    assert await store.summary(GROUP, "topic-a") is None
    assert await store.unsummarized(GROUP, "topic-a") == []
    assert [turn.content["text"] for turn in await store.recent(GROUP, "topic-b")] == ["other"]
    assert [turn.content["text"] for turn in await store.recent(other_chat, None)] == ["private"]
    assert (await store.reset_point(GROUP, "topic-a")).after_turn_id == topic.id
    assert (await store.reset_point(GROUP, "topic-a")).generation == 1
    assert (await store.reset_point(GROUP, "topic-b")) is None
    assert other_topic.id != topic.id


async def test_summary_and_unsummarized_respect_reset_boundary():
    store = ConversationStore()
    actor = UserRef("fake", "alice")
    old = await store.add(GROUP, None, "user", {"text": "old"})
    await store.reset(GROUP, None, by=actor)
    await store.save_summary(GROUP, None, "raced old summary", old.id)
    assert await store.summary(GROUP, None) is None
    assert await store.unsummarized(GROUP, None) == []

    later = await store.add(GROUP, None, "user", {"text": "later"})
    await store.add(GROUP, None, "user", {"text": "latest"})
    assert [turn.id for turn in await store.unsummarized(GROUP, None)] == [later.id, later.id + 1]


async def test_reset_generation_invalidates_summary_racing_provider_call():
    store = ConversationStore()
    for index in range(6):
        await store.add(GROUP, None, "user", {"text": f"pre-reset-{index}"})
    started, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider:
        name = "fake"
        model = "fake-model"

        async def complete(self, *_args, **_kwargs):
            started.set()
            await release.wait()
            return Completion(Message.assistant("stale summary"), "end_turn")

    task = asyncio.create_task(
        store.compact(GROUP, None, WaitingProvider(), "rules", "fake-model", force=True, keep=4)
    )
    await started.wait()
    await store.reset(GROUP, None, by=user().ref)
    release.set()
    await task

    assert await store.summary(GROUP, None) is None
    assert await store.recent(GROUP, None) == []


async def test_compact_keeps_messages_logged_during_provider_call_in_ambient_context():
    stores = memory_stores()
    conversation = ConversationStore()
    for index in range(6):
        await conversation.add(GROUP, None, "user", {"text": f"turn-{index}"})

    class LoggingProvider:
        name = "fake"
        model = "fake-model"

        async def complete(self, *_args, **_kwargs):
            await stores.repo.log_message(message("arrived during compact", sender=user("late")))
            return Completion(Message.assistant("summary"), "end_turn")

    plugin = ConversationPlugin(
        conversation, can_reset=lambda _user, _chat: True, provider=LoggingProvider()
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=True,
        responder=AgentResponder(
            ScriptedProvider([]),
            system_prompt="",
            settings=AgentSettings(),
            limits=Limits(stores.cache),
        ),
    )

    await app.handle(message("/compact@bot"))
    _system, turns, *_ = await app.responder._context(
        message("@bot follow-up", mention=True), "follow-up", app, conversation, "tag"
    )

    assert "arrived during compact" in "\n".join(turn.text for turn in turns)


async def test_retention_of_reset_point_keeps_current_summary_valid():
    store = ConversationStore()
    reset_at = datetime(2020, 1, 1, tzinfo=UTC)
    await store.reset(GROUP, None, by=user().ref, at=reset_at)
    for index in range(6):
        await store.add(GROUP, None, "user", {"text": f"turn-{index}"})
    provider = ScriptedProvider([Completion(Message.assistant("retained summary"), "end_turn")])
    await store.compact(GROUP, None, provider, "rules", "fake-model", force=True, keep=4)
    expected = await store.summary(GROUP, None)
    assert expected is not None

    await store.purge_older_than(reset_at + timedelta(days=1))

    assert await store.reset_point(GROUP, None) is None
    assert await store.summary(GROUP, None) == expected
    assert [turn.content["text"] for turn in await store.recent(GROUP, None)] == [
        "turn-2",
        "turn-3",
        "turn-4",
        "turn-5",
    ]


async def test_ambient_only_compaction_does_not_hide_four_kept_turns():
    stores = memory_stores()
    store = ConversationStore()
    turns = [await store.add(GROUP, None, "user", {"text": f"kept-{index}"}) for index in range(4)]
    await stores.repo.log_message(message("ambient discussion"))
    provider = ScriptedProvider([Completion(Message.assistant("ambient summary"), "end_turn")])
    plugin = ConversationPlugin(
        store, can_reset=lambda _user, _chat: True, bot_name="bot", provider=provider
    )
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=True,
    )
    await app.handle(message("/compact@bot"))

    assert "ambient discussion" in provider.calls[0]["messages"][0].text
    assert [turn.id for turn in await store.recent(GROUP, None)] == [turn.id for turn in turns]
    assert platform.texts and platform.texts[-1].startswith("Compacted 0 turns and 1 chat messages")


async def test_prior_summary_is_not_truncated_during_compaction():
    store = ConversationStore()
    await store.save_summary(GROUP, None, "x" * 3000 + " FINAL SENTENCE", 0)
    ambient = StoredMessage(
        GROUP, "ambient", user().ref, "Alice", "ambient discussion", datetime.now(UTC)
    )
    provider = ScriptedProvider([Completion(Message.assistant("summary"), "end_turn")])

    await store.compact(GROUP, None, provider, "rules", "fake-model", force=True, ambient=[ambient])

    assert "FINAL SENTENCE" in provider.calls[0]["messages"][0].text


async def test_reset_expiry_and_advance_ambient_only_moves_forward():
    store = ConversationStore()
    actor = UserRef("fake", "alice")
    first = datetime(2026, 1, 1, tzinfo=UTC)
    later = first + timedelta(hours=1)
    await store.advance_ambient(GROUP, None, by=actor, at=later)
    await store.advance_ambient(GROUP, None, by=actor, at=first)
    assert (await store.reset_point(GROUP, None)).reset_at == later
    await store.purge_older_than(later + timedelta(seconds=1))
    assert await store.reset_point(GROUP, None) is None


async def test_advance_ambient_preserves_reset_generation_and_turn_boundary():
    store = ConversationStore()
    turn = await store.add(GROUP, None, "user", {"text": "before"})
    point = await store.reset(GROUP, None, by=user().ref)
    advanced = await store.advance_ambient(
        GROUP, None, by=user("bob").ref, at=point.reset_at + timedelta(minutes=1)
    )
    assert advanced.generation == point.generation == 1
    assert advanced.after_turn_id == point.after_turn_id == turn.id


async def test_chat_purge_removes_reset_point_and_turns():
    store = ConversationStore()
    actor = UserRef("fake", "alice")
    await store.add(GROUP, None, "user", {"text": "old"})
    await store.reset(GROUP, None, by=actor)
    await store.purge_chat(GROUP)
    assert await store.reset_point(GROUP, None) is None
    assert await store.recent(GROUP, None) == []


def test_addressed_reset_commands_route_in_mention_only_groups():
    for text, mention in (("/reset@SBot", False), ("@SBot /reset", True), ("@SBot   /reset", True)):
        incoming = message(text, mention=mention)
        assert addressed_command(incoming, "sbot").name == "reset"
        assert (
            decide(incoming, ChatSettings(GROUP), bot_name="sbot", group_mentions_only=True)
            is Route.COMMAND
        )
    assert (
        decide(message("/reset"), ChatSettings(GROUP), bot_name="sbot", group_mentions_only=True)
        is Route.IGNORE
    )
    assert (
        decide(
            message("/reset@otherbot"),
            ChatSettings(GROUP),
            bot_name="sbot",
            group_mentions_only=True,
        )
        is Route.IGNORE
    )
    assert addressed_command(message("@otherbot /reset"), "sbot") is None
    assert addressed_command(message("@SBotX /reset", mention=True), "sbot") is None
    assert (
        decide(
            message("@SBot what is /reset", mention=True),
            ChatSettings(GROUP),
            bot_name="sbot",
            group_mentions_only=True,
        )
        is Route.RESPOND
    )
    assert (
        decide(
            message("/reset", dm=True),
            ChatSettings(GROUP),
            bot_name="sbot",
            group_mentions_only=True,
        )
        is Route.COMMAND
    )


def test_mentioned_path_with_leading_slash_routes_as_before():
    assert (
        decide(
            message(" /var/log/app.log @bot explain this path", mention=True),
            ChatSettings(GROUP),
            bot_name="bot",
            group_mentions_only=True,
        )
        is Route.RESPOND
    )


async def test_agent_filters_old_ambient_but_keeps_explicit_reply():
    stores = memory_stores()
    provider = ScriptedProvider([Completion(Message.assistant("ok"), "end_turn")])
    conversation = ConversationStore()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[],
        bot_name="bot",
        responder=AgentResponder(
            provider, system_prompt="", settings=AgentSettings(), limits=Limits(stores.cache)
        ),
    )
    old = message("old human context")
    await stores.repo.log_message(old)
    await stores.repo.log_bot_message(GROUP, "old-bot", "old bot context", thread_id=None)
    reset_at = datetime.now(UTC) + timedelta(seconds=1)
    await conversation.reset(GROUP, None, by=user().ref, at=reset_at)
    fresh = replace(message("new human context"), sent_at=reset_at + timedelta(seconds=1))
    await stores.repo.log_message(fresh)
    current = replace(
        message("@bot question", mention=True),
        reply_to=MessageRef(GROUP, old.message_id),
    )
    _system, turns, *_ = await app.responder._context(current, "question", app, conversation, "tag")
    combined = "\n".join(turn.text for turn in turns)
    assert combined.count("old human context") == 1
    assert "old bot context" not in combined
    assert "new human context" in combined
    assert '"user_key":"fake:alice"' in combined
    assert len(await stores.repo.recent_messages(GROUP, limit=10)) == 3


async def test_ambient_lines_are_capped_and_newest_are_retained():
    stores = memory_stores()
    settings = AgentSettings(ambient_message_chars=1500, ambient_total_chars=6000)
    responder = AgentResponder(
        ScriptedProvider([]), system_prompt="", settings=settings, limits=Limits(stores.cache)
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        bot_name="bot",
    )
    paste = replace(message("x" * 10_000), message_id="paste", user=user("paste"))
    await stores.repo.log_message(paste)
    _system, turns, *_ = await responder._context(
        message("question"), "question", app, ConversationStore(), "tag"
    )
    ambient = next(turn.text for turn in turns if "x" * 100 in turn.text)
    assert "x" * 1500 + "\\u2026[truncated]" in ambient
    assert "x" * 1501 not in ambient

    stores = memory_stores()
    responder = AgentResponder(
        ScriptedProvider([]), system_prompt="", settings=settings, limits=Limits(stores.cache)
    )
    app.stores = stores
    for index in range(8):
        row = replace(message(f"row-{index}-" + str(index) * 1490), user=user(f"u{index}"))
        await stores.repo.log_message(row)
    _system, turns, *_ = await responder._context(
        message("question"), "question", app, ConversationStore(), "tag"
    )
    ambient = next(turn.text for turn in turns if "row-7-" in turn.text)
    assert "row-7-" in ambient and "row-6-" in ambient
    assert "row-0-" not in ambient
    payload = ambient.split(">", 1)[1].rsplit("</untrusted-", 1)[0]
    assert len(payload) <= 6_000


def test_agent_settings_preserves_positional_field_order():
    settings = AgentSettings(2048, False, 20, 12, 8, 60)
    assert settings.recent_turns == 12


async def test_auto_compaction_keeps_last_twelve_verbatim():
    store = ConversationStore()
    provider = ScriptedProvider([Completion(Message.assistant("summary"), "end_turn")])
    for index in range(31):
        await store.add(GROUP, None, "user", {"text": f"turn {index}"})
    await store.compact(GROUP, None, provider, "rules", "fake-model")
    assert [turn.content["text"] for turn in await store.recent(GROUP, None)] == [
        f"turn {index}" for index in range(19, 31)
    ]


async def test_compact_after_reset_never_sends_pre_reset_turns_to_provider():
    store = ConversationStore()
    actor = user().ref
    for index in range(8):
        await store.add(GROUP, None, "user", {"text": f"pre-reset-{index}"})
    point = await store.reset(GROUP, None, by=actor)
    for index in range(6):
        await store.add(GROUP, None, "user", {"text": f"post-reset-{index}"})
    ambient = [
        StoredMessage(
            GROUP,
            "old-ambient",
            actor,
            "Alice",
            "pre-reset ambient",
            point.reset_at - timedelta(seconds=1),
        ),
        StoredMessage(
            GROUP,
            "new-ambient",
            actor,
            "Alice",
            "post-reset ambient",
            point.reset_at + timedelta(seconds=1),
        ),
    ]
    provider = ScriptedProvider([Completion(Message.assistant("new summary"), "end_turn")])

    result = await store.compact(
        GROUP, None, provider, "rules", "fake-model", force=True, keep=4, ambient=ambient
    )

    transcript = provider.calls[0]["messages"][0].text
    assert "pre-reset" not in transcript
    assert "post-reset ambient" in transcript
    assert "post-reset-0" in transcript and "post-reset-1" in transcript
    assert result.turns_summarized == 2
    assert [turn.content["text"] for turn in await store.recent(GROUP, None)] == [
        "post-reset-2",
        "post-reset-3",
        "post-reset-4",
        "post-reset-5",
    ]


async def test_ambient_only_compaction_survives_reset_and_does_not_hide_next_turn():
    store = ConversationStore()
    actor = user().ref
    point = await store.reset(GROUP, None, by=actor)
    ambient = StoredMessage(
        GROUP,
        "ambient-1",
        actor,
        "Alice",
        "new chat discussion",
        point.reset_at + timedelta(seconds=1),
    )
    provider = ScriptedProvider([Completion(Message.assistant("ambient summary"), "end_turn")])
    await store.compact(GROUP, None, provider, "rules", "fake-model", force=True, ambient=[ambient])
    assert await store.summary(GROUP, None) == ("ambient summary", point.after_turn_id)
    later = await store.add(GROUP, None, "user", {"text": "next turn"})
    assert [turn.id for turn in await store.recent(GROUP, None)] == [later.id]


async def test_user_purge_and_retention_still_remove_kept_turns_and_reset_rows():
    store = ConversationStore()
    old = await store.add(GROUP, None, "user", {"text": "old"}, "message-1")
    await store.reset(GROUP, None, by=user().ref, at=old.created_at)
    await store.purge_user(GROUP, user().ref, {"message-1"})
    assert store._turns == []
    assert await store.reset_point(GROUP, None) is not None
    await store.purge_older_than(old.created_at + timedelta(seconds=1))
    assert await store.reset_point(GROUP, None) is None


async def test_manual_compact_is_empty_safe_cooldown_scoped_and_keeps_four_turns():
    stores = memory_stores()
    conversation = ConversationStore()
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("summary one"), "end_turn", Usage(20, 10)),
            Completion(Message.assistant("summary two"), "end_turn", Usage(40, 10)),
        ]
    )
    limits = Limits(stores.cache)
    plugin = ConversationPlugin(
        conversation, limits, can_reset=lambda _user, _chat: True, bot_name="bot", provider=provider
    )
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key, "fake:other"], []),
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=True,
    )

    await app.handle(message("/compact@bot"))
    assert platform.texts[-1] == "Nothing to compact yet."
    assert provider.calls == []
    for index in range(6):
        await conversation.add(GROUP, None, "user", {"text": f"turn-{index}"})
    await app.handle(message("/compact@bot"))
    assert len(provider.calls) == 1
    assert not provider.calls[0]["tools"]
    assert [turn.content["text"] for turn in await conversation.recent(GROUP, None)] == [
        "turn-2",
        "turn-3",
        "turn-4",
        "turn-5",
    ]
    assert await limits.budget_used() == 30
    assert len(conversation._usage) == 1
    await app.handle(message("@bot /compact", mention=True))
    assert len(provider.calls) == 1
    assert "recently" in platform.texts[-1]
    other = ChatRef("fake", "other")
    for index in range(6):
        await conversation.add(other, None, "user", {"text": f"other-{index}"})
    await app.handle(message("/compact@bot", chat=other))
    assert len(provider.calls) == 2
    assert await limits.budget_used() == 80
    assert len(conversation._usage) == 2


async def test_manual_compact_honors_rate_limit_without_calling_provider():
    stores = memory_stores()
    conversation = ConversationStore()
    provider = ScriptedProvider([Completion(Message.assistant("summary"), "end_turn")])

    class DenyLimits:
        async def check(self, *_args):
            return "rate limited"

        async def record(self, *_args):
            raise AssertionError("rate-limited compaction must not record usage")

    for index in range(6):
        await conversation.add(GROUP, None, "user", {"text": f"turn-{index}"})
    plugin = ConversationPlugin(
        conversation,
        DenyLimits(),
        can_reset=lambda _user, _chat: True,
        provider=provider,
    )
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=True,
    )
    await app.handle(message("/compact@bot"))
    assert platform.texts == ["rate limited"]
    assert provider.calls == []


async def test_manual_compact_drop_keeps_previous_summary_and_does_not_advance_ambient():
    stores = memory_stores()
    conversation = ConversationStore()
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("cut"), "max_tokens", Usage(3, 4)),
            Completion(Message.assistant("still cut"), "max_tokens", Usage(5, 6)),
        ]
    )
    for index in range(6):
        await conversation.add(GROUP, None, "user", {"text": f"turn-{index}"})
    await conversation.save_summary(GROUP, None, "previous", 0)
    plugin = ConversationPlugin(
        conversation,
        Limits(stores.cache),
        can_reset=lambda _user, _chat: True,
        provider=provider,
    )
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=True,
    )
    await app.handle(message("/compact@bot"))
    assert platform.texts == [
        "I couldn't fit a summary this time, so I kept the previous one. Try again later."
    ]
    assert await conversation.summary(GROUP, None) == ("previous", 0)
    assert await conversation.reset_point(GROUP, None) is None
    assert len(conversation._usage) == 1


async def test_legacy_compact_ambient_retry_success_advances_ambient():
    stores = memory_stores()
    conversation = ConversationStore()
    actor = user()
    row = message("ambient fact", sender=actor)
    await stores.repo.log_message(row)
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("partial"), "max_tokens", Usage(2, 3)),
            Completion(Message.assistant("ambient summary"), "end_turn", Usage(4, 5)),
        ]
    )
    plugin = ConversationPlugin(
        conversation,
        Limits(stores.cache),
        can_reset=lambda _user, _chat: True,
        provider=provider,
    )
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=True,
    )
    await app.handle(message("/compact@bot"))
    assert platform.texts[0].startswith("Compacted 0 turns and 1 chat messages")
    assert await conversation.summary(GROUP, None) == ("ambient summary", 0)
    assert await conversation.reset_point(GROUP, None) is not None
    assert len(provider.calls) == 2


async def test_reset_moderation_matches_access_policy_and_dm_user_can_reset():
    owner, moderator, full_admin = user("owner"), user("moderator"), user("admin")
    setup, other_mod, member = user("setup"), user("other_mod"), user("member")
    stores = memory_stores()
    admin_store = MemoryAdministrationStore(stores.repo)
    other = ChatRef("fake", "other")
    changes = [
        AdminChange("moderator_grant", GROUP, moderator.ref),
        AdminChange("grant", GROUP, setup.ref),
        AdminChange("moderator_grant", other, other_mod.ref),
    ]
    for revision, change in enumerate(changes):
        await admin_store.apply(
            actor=owner.ref,
            owners=frozenset({owner.ref.key}),
            expected_revision=revision,
            change=change,
        )
    access = AccessPolicy(
        [GROUP.key, other.key],
        [full_admin.ref.key],
        owners=[owner.ref.key],
        store=admin_store,
    )
    await access.refresh()
    platform = FakePlatform(members={GROUP.key: {member.ref.key}})
    conversation = ConversationStore()
    plugin = ConversationPlugin(conversation, can_reset=access.can_moderate, bot_name="bot")
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=True,
    )

    for actor in (owner, moderator):
        await app.handle(message("/reset@bot", sender=actor))
        assert "Context reset." in platform.texts[-1]
        assert "/memory@bot" in platform.texts[-1] and "/forget@bot" in platform.texts[-1]
    for actor in (full_admin, setup, other_mod, member):
        before = len(platform.texts)
        await app.handle(message("/reset@bot", sender=actor))
        assert len(platform.texts) == before + 1
        assert "Only the owner" in platform.texts[-1]
        before = len(platform.texts)
        await app.handle(message("/reset@bot", sender=actor))
        assert len(platform.texts) == before
    await app.handle(message("/reset", sender=member, dm=True))
    assert "Context reset." in platform.texts[-1]
    assert "/memory@bot" not in platform.texts[-1] and "/memory " in platform.texts[-1]


async def test_restricted_group_reset_gets_restriction_notice_before_handler():
    owner, blocked = user("owner"), user("blocked")
    stores = memory_stores()
    admin_store = MemoryAdministrationStore(stores.repo)
    await admin_store.apply(
        actor=owner.ref,
        owners=frozenset({owner.ref.key}),
        expected_revision=0,
        change=AdminChange("block", GROUP, blocked.ref),
    )
    access = AccessPolicy([GROUP.key], [], owners=[owner.ref.key], store=admin_store)
    platform = FakePlatform()
    conversation = ConversationStore()
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[ConversationPlugin(conversation, can_reset=access.can_moderate)],
        bot_name="bot",
        group_mentions_only=True,
    )
    await app.handle(message("/reset@bot", sender=blocked))
    assert platform.texts == ["You can't use this bot here right now."]
    assert await conversation.reset_point(GROUP, None) is None
