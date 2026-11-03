"""Authorization, exact proposals, idempotent confirmation and native vote handling."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.store import AdminChange, MemoryAdministrationStore
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.display_time_store import DisplayTimeService, MemoryDisplayTimeStore, display_service
from chatcore.events import ButtonPress, ChatRef, MessageRef, PollAnswer, User, UserRef
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.polls import PollAnswerListener
from chatcore.testing import FakePlatform, ScriptedProvider, memory_stores, message, user
from chatcore.tools.registry import Tool, ToolContext, ToolRegistry, ToolResult
from chatcore_polls.repository import PollsRepo
from chatcore_polls.settings import PollsSettings


async def _no_limit(*_args):
    return None


async def _record(*_args):
    return None


async def _no_budget(*_args):
    return False


def _tool_call(name, call_id, args=None):
    return Completion(
        Message.assistant(
            "",
            (
                ToolCall(
                    call_id,
                    name,
                    {"question": "Dinner?", "options": ["Ramen", "Tacos"]}
                    if args is None
                    else args,
                ),
            ),
        ),
        "tool_use",
    )


async def command(plugin, ctx, name, args=""):
    return await next(c for c in plugin.commands() if c.name == name).handler(ctx, args)


async def propose(plugin, ctx, **args):
    tool_ctx = ToolContext(
        ctx.chat, ctx.user, ctx.is_dm, ctx.platform, ctx.stores, thread_id=ctx.thread_id
    )
    return await ToolRegistry(plugin.tools()).call(
        "create_poll", {"question": "Dinner?", "options": ["Ramen", "Tacos"], **args}, tool_ctx
    )


def test_repository_requires_postgres():
    with pytest.raises(TypeError):
        PollsRepo(object(), PollsSettings())


async def test_direct_command_posts_native_poll_once_with_no_extra_reply(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    assert await command(plugin, ctx, "poll", "Dinner? | Ramen | Tacos") is None
    sent = ctx.platform.polls
    assert len(sent) == 1 and sent[0][:4] == ("Dinner?", ("Ramen", "Tacos"), False, False)
    assert sent[0][-1].message.thread_id == "7"
    poll = next(iter(repo.rows.values()))
    assert poll.status == "open"
    history = await ctx.stores.repo.get_message(ctx.chat, poll.platform_message_id)
    assert history.thread_id == "7" and history.text == "Poll #1: Dinner? — options: Ramen | Tacos"


async def test_scheduled_command_label_and_history_error_is_safe(setup_poll, caplog):
    plugin, ctx, _, _ = setup_poll
    ctx.stores.repo.log_bot_message = AsyncMock(side_effect=RuntimeError("private content"))
    reply = await command(plugin, ctx, "poll", "--close 2026-11-01T12:00:00 UTC Q | A | B")
    assert reply.startswith("Poll #1 closes at 2026-11-01 12:00:00 UTC")
    assert "RuntimeError" in caplog.text and "private content" not in caplog.text


async def test_poll_list_formats_close_time_in_effective_display_zone(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    await command(plugin, ctx, "poll", "--close 2026-11-01T12:00:00 UTC Q | A | B")
    await display_service(ctx.stores).set_zone(
        ctx.chat, ctx.user.ref, is_dm=False, zone="Asia/Singapore"
    )

    result = await command(plugin, ctx, "polls")

    assert "2026-11-01 20:00:00 Asia/Singapore (UTC+08:00)" in result
    assert next(iter(repo.rows.values())).close_at == datetime(2026, 11, 1, 12, tzinfo=UTC)


async def test_platform_error_marks_failed_and_does_not_log_payload(setup_poll, caplog):
    plugin, ctx, repo, _ = setup_poll
    ctx.platform.send_error = True
    assert await command(plugin, ctx, "poll", "Dinner? | A | B") == (
        "Couldn't send the poll; nothing was posted."
    )
    assert next(iter(repo.rows.values())).status == "failed"
    assert "private" not in caplog.text


@pytest.mark.parametrize(
    "name,args", [("poll", "Q | A | B"), ("polls", ""), ("closepoll", "1"), ("polltools", "on")]
)
async def test_every_command_rejects_dm_and_console(setup_poll, name, args):
    plugin, ctx, repo, _ = setup_poll
    assert (
        await command(plugin, replace(ctx, is_dm=True), name, args) == "Polls work in group chats."
    )
    plugin.service.platform = FakePlatform()
    assert await command(plugin, ctx, name, args) == "Polls are not supported on this platform."
    assert not repo.rows and not ctx.platform.polls


async def test_default_off_and_allowlist_and_tool_dm_rejection(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    assert (await propose(plugin, replace(ctx, is_dm=True))).content == "Polls work in group chats."
    repo.config = {"polls_enabled": False, "revision": 0}
    result = await propose(plugin, ctx)
    assert result.is_error and result.content == (
        "Polls are off in this group. A group setup manager or admin can turn them on: they "
        "can ask me to turn on polls, or send /polltools on."
    )
    plugin.access.bootstrap_chats = frozenset()
    assert (await propose(plugin, ctx)).is_error
    assert not ctx.platform.polls and not repo.rows


async def test_tool_exact_summary_only_proposes_and_commit_is_idempotent(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    result = await propose(plugin, ctx)
    assert result.content == (
        "Post a named, single-choice poll in fake:group topic 7:\n"
        "Question: Dinner?\n1. Ramen\n2. Tacos\nCloses: when closed manually"
    )
    assert result.pending_action.summary == result.content and not result.is_error
    assert not ctx.platform.polls and not repo.rows
    handler = plugin.action_handlers()["create"]
    assert await handler.commit(result.pending_action, ctx) == (
        "Poll #1 posted. To close it early, reply to it with /closepoll or use /closepoll 1."
    )
    await handler.commit(result.pending_action, ctx)
    assert len(ctx.platform.polls) == 1
    create_tool = next(item for item in plugin.tools() if item.name == "create_poll")
    assert create_tool.writes and create_tool.direct


async def test_commit_echo_includes_close_label(setup_poll):
    plugin, ctx, _, _ = setup_poll
    result = await propose(
        plugin,
        ctx,
        close_local_time="2026-11-01T12:00:00",
        close_timezone="UTC",
    )
    echo = await plugin.action_handlers()["create"].commit(result.pending_action, ctx)
    assert "Poll #1 posted; it closes at 2026-11-01 12:00:00 UTC (UTC+00:00;" in echo
    assert "/closepoll 1" in echo
    assert len(ctx.platform.polls) == 1


async def test_agent_commits_clean_poll_directly_and_tainted_poll_needs_confirmation(
    setup_poll,
):
    plugin, _, _, _ = setup_poll
    provider = ScriptedProvider(
        [_tool_call("create_poll", "clean-poll"), Completion(Message.assistant(""), "end_turn")]
    )
    platform = plugin.service.platform
    stores = memory_stores()
    stores.extra["display_time_service"] = DisplayTimeService(MemoryDisplayTimeStore())
    app = BotApp(
        platform,
        stores=stores,
        access=plugin.access,
        plugins=[plugin],
        bot_name="bot",
        responder=AgentResponder(
            provider,
            system_prompt="rules",
            settings=AgentSettings(recent_context_messages=0),
            limits=type(
                "Limits",
                (),
                {"check": _no_limit, "record": _record, "budget_exhausted": _no_budget},
            )(),
        ),
    )
    await app.handle(message("@bot post a poll", mention=True))
    assert len(platform.polls) == 1, [item.text for item in platform.sent]
    assert not app.stores.repo._pending
    assert any("Poll #1 posted" in item.text for item in platform.sent)

    taint = []

    async def fake_web(_ctx):
        taint.append(_ctx.turn)
        return ToolResult("untrusted body", untrusted=True)

    app.tools.register(
        Tool("fake_web", "Untrusted source", {"type": "object", "properties": {}}, fake_web)
    )
    tainted_provider = ScriptedProvider(
        [
            _tool_call("fake_web", "read", {}),
            _tool_call("create_poll", "tainted-poll"),
        ]
    )
    app.responder = AgentResponder(
        tainted_provider,
        system_prompt="rules",
        settings=AgentSettings(recent_context_messages=0),
        limits=type(
            "Limits",
            (),
            {"check": _no_limit, "record": _record, "budget_exhausted": _no_budget},
        )(),
    )
    await app.handle(message("@bot use the page and post a poll", mention=True))
    assert taint[0].read_untrusted and taint[0].untrusted_tools == {"fake_web"}
    assert len(platform.polls) == 1
    assert len(app.stores.repo._pending) == 1
    assert platform.sent[-1].action_id


async def test_tool_summary_includes_exact_close_and_modes(setup_poll):
    plugin, ctx, _, _ = setup_poll
    result = await propose(
        plugin,
        ctx,
        multiple=True,
        anonymous=True,
        close_local_time="2026-11-05T18:00:00",
        close_timezone="Asia/Singapore",
    )
    assert "anonymous, multiple-choice" in result.content
    assert "Closes: 2026-11-05 18:00:00 Asia/Singapore (UTC+08:00; " in result.content
    assert result.pending_action.payload["draft"]["close_at"] == "2026-11-05T10:00:00+00:00"


@pytest.mark.parametrize(
    "args",
    [
        {"close_local_time": "2026-11-01T12:00:00"},
        {"close_timezone": "UTC"},
        {"multiple": "true"},
        {"anonymous": 1},
        {"options": "A,B"},
        {"question": 2},
        {"chat_key": "fake:foreign"},
        {"requester": "fake:owner"},
        {"thread_id": "8"},
        {"options": ["A", 1]},
    ],
)
async def test_tool_untrusted_arguments_are_rejected_without_writes(setup_poll, args):
    plugin, ctx, repo, _ = setup_poll
    result = await propose(plugin, ctx, **args)
    assert result.is_error and result.pending_action is None
    assert not repo.rows and not ctx.platform.polls


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "revision",
        "time",
        "chat",
        "requester",
        "topic",
        "dm",
        "payload_scope",
        "payload_actor",
        "payload_time",
    ],
)
async def test_commit_rechecks_all_scope_settings_and_time(setup_poll, change):
    plugin, ctx, repo, now = setup_poll
    result = await propose(
        plugin, ctx, close_local_time="2026-11-01T12:00:00", close_timezone="UTC"
    )
    action = result.pending_action
    if change == "disabled":
        repo.config["polls_enabled"] = False
    elif change == "revision":
        repo.config["revision"] += 1
    elif change == "time":
        now[0] += timedelta(days=2)
    elif change == "chat":
        ctx.chat = ChatRef("fake", "foreign")
    elif change == "requester":
        ctx.user = user("bob")
    elif change == "topic":
        ctx.thread_id = "8"
    elif change == "dm":
        ctx.is_dm = True
    elif change == "payload_scope":
        action.payload["scope"]["thread_id"] = "8"
    elif change == "payload_actor":
        action.payload["requester"] = "fake:owner"
    elif change == "payload_time":
        action.payload["draft"]["close_at"] = "2026-11-02T12:00:00+00:00"
    reply = await plugin.action_handlers()["create"].commit(action, ctx)
    assert "Nothing was posted" in reply
    assert not repo.rows and not ctx.platform.polls


@pytest.mark.parametrize("manager", ["owner", "delegate", "native"])
async def test_group_management_owner_delegate_or_live_admin(setup_poll, manager):
    plugin, ctx, repo, _ = setup_poll
    if manager == "owner":
        ctx.user = user("owner")
    elif manager == "native":
        ctx.platform.admins.add((ctx.chat.key, ctx.user.ref.key))
    else:
        store = MemoryAdministrationStore(ctx.stores.repo)
        await store.apply(
            actor=user("owner").ref,
            owners=frozenset({user("owner").ref.key}),
            expected_revision=0,
            change=AdminChange("grant", ctx.chat, user=ctx.user.ref),
        )
        plugin.access.store = store
    action = await plugin.service.request_configure(ctx, True)
    assert "on" in action.summary
    assert (
        await plugin.action_handlers()["configure"].commit(action, ctx)
        == "Polls are on in this group."
    )
    assert repo.config["revision"] == 2


async def test_manager_rechecked_after_proposal_and_admin_errors_fail_closed(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    assert "manager" in await command(plugin, ctx, "polltools", "on")
    ctx.platform.admins.add((ctx.chat.key, ctx.user.ref.key))
    action = await plugin.service.request_configure(ctx, False)
    ctx.platform.admin_error = True
    assert "Nothing was posted" in await plugin.action_handlers()["configure"].commit(action, ctx)
    assert repo.config["polls_enabled"]
    ctx.platform.admin_error = False
    repo.config["revision"] += 1
    assert "Nothing was posted" in await plugin.action_handlers()["configure"].commit(action, ctx)


async def test_global_admin_is_not_group_manager(setup_poll):
    plugin, ctx, _, _ = setup_poll
    plugin.access = plugin.service.access = AccessPolicy([ctx.chat.key], [ctx.user.ref.key])
    assert "manager" in await command(plugin, ctx, "polltools", "on")


async def test_configure_goes_through_confirmation_and_off_keeps_open_polls(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    await command(plugin, ctx, "poll", "Q | A | B")
    ctx.user = user("owner")
    ctx.confirm = AsyncMock()
    assert await command(plugin, ctx, "polltools", "off") is None
    action = ctx.confirm.request.call_args.args[0]
    assert action.kind == "configure" and repo.config["polls_enabled"]
    await plugin.action_handlers()["configure"].commit(action, ctx)
    assert not repo.config["polls_enabled"]
    assert next(iter(repo.rows.values())).status == "open"
    assert "#1" in await command(plugin, ctx, "polls")


async def test_named_answers_replace_retract_and_username_fallback(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    assert isinstance(plugin, PollAnswerListener)
    await command(plugin, ctx, "poll", "Q | A | B")
    poll = next(iter(repo.rows.values()))
    voter = User(UserRef("fake", "bob"), "", username="bob")
    for indices in [(0,), (1,), ()]:
        await plugin.on_poll_answer(PollAnswer(poll.platform_poll_id, voter, indices), ctx.stores)
        assert [v["option_index"] for v in repo.ballots] == list(indices)
        if indices:
            assert repo.ballots[0]["voter_name"] == "@bob"
    assert not ctx.platform.sent


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "closed",
        "anonymous",
        "disallowed",
        "range",
        "negative",
        "multi",
        "duplicate",
        "other_platform",
    ],
)
async def test_invalid_or_unavailable_answers_are_silent(setup_poll, case):
    plugin, ctx, repo, _ = setup_poll
    await command(plugin, ctx, "poll", "Q | A | B")
    poll = next(iter(repo.rows.values()))
    indices = (0,)
    poll_id, voter = poll.platform_poll_id, user()
    if case in {"closed", "anonymous"}:
        repo.rows[poll.id] = replace(
            poll, **({"status": "closed"} if case == "closed" else {"anonymous": True})
        )
    elif case == "unknown":
        poll_id = "unknown"
    elif case == "disallowed":
        plugin.access.bootstrap_chats = frozenset()
    elif case == "other_platform":
        voter = User(UserRef("tg", "alice"), "Alice")
    else:
        indices = {"range": (2,), "negative": (-1,), "multi": (0, 1), "duplicate": (0, 0)}[case]
    await plugin.on_poll_answer(PollAnswer(poll_id, voter, indices), ctx.stores)
    assert repo.ballots == [] and not ctx.platform.sent


async def test_multichoice_answers_accept_multiple_options(setup_poll):
    plugin, ctx, repo, _ = setup_poll
    await command(plugin, ctx, "poll", "--multi Q | A | B")
    poll = next(iter(repo.rows.values()))
    await plugin.on_poll_answer(PollAnswer(poll.platform_poll_id, user(), (0, 1)), ctx.stores)
    assert len(repo.ballots) == 2


async def test_real_core_button_confirmation_preserves_topic(setup_poll):
    plugin, ctx, repo, now = setup_poll
    # This exercises topic propagation, so keep the pending-action expiry after wall-clock now.
    now[0] = datetime.now(UTC)
    app = BotApp(
        ctx.platform, stores=ctx.stores, access=plugin.access, plugins=[plugin], bot_name="bot"
    )
    result = await propose(plugin, ctx)
    await app.confirm.request(result.pending_action, ctx.user)
    await app.handle(
        ButtonPress(
            ctx.chat,
            ctx.user,
            result.pending_action.id,
            "confirm",
            MessageRef(ctx.chat, "confirmation", "7"),
        )
    )
    assert len(ctx.platform.polls) == 1
    assert next(iter(repo.rows.values())).scope.thread_id == "7"


async def test_privacy_hooks_forward_only_required_scope(setup_poll):
    from types import SimpleNamespace

    plugin, ctx, _, now = setup_poll
    repo = SimpleNamespace(
        purge_user=AsyncMock(), purge_chat=AsyncMock(), purge_older_than=AsyncMock()
    )
    plugin.repo_for_privacy = lambda stores: repo
    await plugin.purge_user(ctx.chat, ctx.user.ref, ctx.stores)
    await plugin.purge_chat(ctx.chat, ctx.stores)
    await plugin.purge_older_than(now[0], ctx.stores)
    repo.purge_user.assert_awaited_once_with(ctx.chat.key, ctx.user.ref.key)
    repo.purge_chat.assert_awaited_once_with(ctx.chat.key)
    repo.purge_older_than.assert_awaited_once_with(now[0])


@pytest.mark.parametrize(
    "args",
    [
        {"close_local_time": None},
        {"close_timezone": None},
        {"close_local_time": None, "close_timezone": None},
        {"close_local_time": "", "close_timezone": ""},
    ],
)
async def test_tool_explicit_null_close_fields_do_not_bypass_schema(setup_poll, args):
    plugin, ctx, repo, _ = setup_poll
    result = await propose(plugin, ctx, **args)
    assert result.is_error and result.pending_action is None
    assert not repo.rows and not ctx.platform.polls


async def test_concurrent_confirmation_during_sending_cannot_post_twice(setup_poll):
    import asyncio

    plugin, ctx, _, _ = setup_poll
    result = await propose(plugin, ctx)
    entered, release = asyncio.Event(), asyncio.Event()
    send = ctx.platform.send_poll

    async def blocked_send(*args, **kwargs):
        entered.set()
        await release.wait()
        return await send(*args, **kwargs)

    ctx.platform.send_poll = blocked_send
    handler = plugin.action_handlers()["create"]
    first = asyncio.create_task(handler.commit(result.pending_action, ctx))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        assert "already handled" in await handler.commit(result.pending_action, ctx)
        assert not ctx.platform.polls
    finally:
        release.set()
        await first
    assert len(ctx.platform.polls) == 1


async def test_privacy_plugin_reaches_poll_hooks(setup_poll):
    from types import SimpleNamespace

    from chatcore.privacy import PrivacyPlugin

    plugin, ctx, _, _ = setup_poll
    repo = SimpleNamespace(purge_user=AsyncMock())
    plugin.repo_for_privacy = lambda stores: repo
    privacy = PrivacyPlugin([plugin], provider_label="test")
    _, failed = await privacy._run_purges("purge_user", ctx.stores, ctx.chat, ctx.user.ref)
    assert not failed
    repo.purge_user.assert_awaited_once_with(ctx.chat.key, ctx.user.ref.key)


async def test_delegated_manager_is_refreshed_and_revocation_blocks_commit(setup_poll):
    plugin, ctx, _, _ = setup_poll
    store = MemoryAdministrationStore()
    owner = user("owner").ref
    granted = await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("grant", ctx.chat, user=ctx.user.ref),
    )
    plugin.access.store = store
    action = await plugin.service.request_configure(ctx, False)
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=granted.revision,
        change=AdminChange("revoke", ctx.chat, user=ctx.user.ref),
    )
    assert "Nothing was posted" in await plugin.action_handlers()["configure"].commit(action, ctx)


async def test_owner_permission_fails_closed_when_access_refresh_fails(setup_poll):
    plugin, ctx, _, _ = setup_poll
    ctx.user = user("owner")
    plugin.access.refresh = AsyncMock(side_effect=RuntimeError("private snapshot error"))
    assert "manager" in await command(plugin, ctx, "polltools", "off")


@pytest.mark.parametrize("non_utc_storage", [False, True])
async def test_open_poll_list_formats_scheduled_close_in_utc(setup_poll, non_utc_storage):
    from datetime import timezone

    plugin, ctx, repo, _ = setup_poll
    await command(
        plugin, ctx, "poll", "--close 2026-11-05T18:00:00 Asia/Singapore Dinner? | Ramen | Tacos"
    )
    if non_utc_storage:
        poll = next(iter(repo.rows.values()))
        repo.rows[poll.id] = replace(
            poll, close_at=poll.close_at.astimezone(timezone(timedelta(hours=8)))
        )
    assert await command(plugin, ctx, "polls") == (
        "#1 · Dinner? · single · named · closes 2026-11-05 10:00:00 UTC (UTC+00:00)"
    )
