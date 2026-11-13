"""Natural-language inviter changes must remain owner-confirmed and atomic."""

import pytest
from chatcore.administration.store import AdminChange, MemoryAdministrationStore
from chatcore.events import ChatRef, User, UserRef

OWNER = UserRef("tg", "1")
FRIENDS = tuple(UserRef("tg", str(i)) for i in range(2, 7))
OWNERS = frozenset({OWNER.key})


async def test_five_inviter_grants_are_one_revision_and_deduplicated_audit():
    store = MemoryAdministrationStore()
    snapshot = await store.apply_inviter_batch(
        actor=OWNER,
        owners=OWNERS,
        expected_revision=0,
        operation="trust",
        users=(*FRIENDS, FRIENDS[0]),
    )
    assert snapshot.trusted_inviters == frozenset(user.key for user in FRIENDS)
    assert snapshot.revision == 1
    audit = await store.audit()
    assert len(audit) == 5 and {row.revision for row in audit} == {1}
    unchanged = await store.apply_inviter_batch(
        actor=OWNER,
        owners=OWNERS,
        expected_revision=1,
        operation="trust",
        users=FRIENDS,
    )
    assert unchanged == snapshot and len(await store.audit()) == 5
    revoked = await store.apply_inviter_batch(
        actor=OWNER,
        owners=OWNERS,
        expected_revision=1,
        operation="untrust",
        users=FRIENDS,
    )
    assert not revoked.trusted_inviters and revoked.revision == 2


@pytest.mark.parametrize("bad", [UserRef("tg", "*"), UserRef("other", "2"), OWNER])
async def test_invalid_batch_target_does_not_apply_earlier_valid_targets(bad):
    store = MemoryAdministrationStore()
    with pytest.raises((ValueError, PermissionError)):
        await store.apply_inviter_batch(
            actor=OWNER,
            owners=OWNERS,
            expected_revision=0,
            operation="trust",
            users=(FRIENDS[0], bad),
        )
    assert (await store.snapshot()).revision == 0
    assert not (await store.snapshot()).trusted_inviters
    assert await store.audit() == ()


async def test_batch_is_owner_only_and_cannot_mutate_other_permissions():
    store = MemoryAdministrationStore()
    with pytest.raises(PermissionError):
        await store.apply_inviter_batch(
            actor=FRIENDS[0],
            owners=OWNERS,
            expected_revision=0,
            operation="trust",
            users=(FRIENDS[1],),
        )
    for operation in ("grant", "enable", "moderator_grant"):
        with pytest.raises(ValueError):
            await store.apply_inviter_batch(
                actor=OWNER,
                owners=OWNERS,
                expected_revision=0,
                operation=operation,
                users=FRIENDS,
            )
    assert (await store.snapshot()).revision == 0


async def make_plugin():
    from chatcore.access import AccessPolicy
    from chatcore.administration.plugin import AdministrationPlugin
    from chatcore.testing import FakePlatform, memory_stores
    from chatcore.tools.registry import ToolContext

    group = ChatRef("tg", "-1001")
    platform = FakePlatform(name="tg")
    platform.members[group.key] = {OWNER.key, *(u.key for u in FRIENDS)}
    stores = memory_stores()
    names = ("Alex", "Ben", "Chris", "Dana", "Eli")
    for ref, name in zip(FRIENDS, names, strict=True):
        await stores.repo.upsert_member(group, User(ref, display_name=name, username=name.lower()))
    access = AccessPolicy([group.key], [], owners=OWNERS)
    store = MemoryAdministrationStore(stores.repo)
    access.store = store
    await access.refresh()
    plugin = AdministrationPlugin(access, store, OWNERS)
    context = ToolContext(
        ChatRef("tg", "1"), User(OWNER, display_name="Owner"), True, platform, stores, is_admin=True
    )
    return plugin, context, names, group


async def request(plugin, context, people, operation="trust"):
    tools = {tool.name: tool for tool in plugin.tools()}
    assert "manage_trusted_inviters" in tools
    tool = tools["manage_trusted_inviters"]
    assert tool.writes and not tool.direct
    return await tool.handler(context, operation=operation, people=people)


async def test_name_batch_previews_five_people_then_commits_once_in_same_owner_dm():
    from chatcore.plugin import CommandContext

    plugin, ctx, names, _ = await make_plugin()
    result = await request(plugin, ctx, list(names))
    assert not result.is_error and result.pending_action
    assert not (await plugin.store.snapshot()).trusted_inviters
    for name, ref in zip(names, FRIENDS, strict=True):
        assert name in result.content and ref.key in result.content
    command = CommandContext(ctx.chat, ctx.user, True, True, ctx.platform, ctx.stores)
    response = await plugin.commit(result.pending_action, command)
    assert "5" in response
    assert (await plugin.store.snapshot()).trusted_inviters == frozenset(u.key for u in FRIENDS)
    assert (await plugin.store.snapshot()).revision == 1


async def test_unknown_and_ambiguous_people_never_stage_partial_batch():
    plugin, ctx, names, group = await make_plugin()
    unknown = await request(plugin, ctx, [names[0], "Unknown"])
    assert unknown.is_error and unknown.pending_action is None
    await ctx.stores.repo.upsert_member(
        group, User(UserRef("tg", "20"), display_name="Alex", username="otheralex")
    )
    ambiguous = await request(plugin, ctx, ["Alex", "Ben"])
    assert ambiguous.is_error and ambiguous.pending_action is None
    assert "Ambiguous" in ambiguous.content
    assert not (await plugin.store.snapshot()).trusted_inviters
    exact = await request(plugin, ctx, ["@alex", "Ben"])
    assert exact.pending_action


async def test_names_require_requesters_shared_group_and_no_bot_targets():
    plugin, ctx, names, group = await make_plugin()
    await ctx.stores.repo.upsert_member(
        group, User(UserRef("tg", "20"), display_name="Helperbot", is_bot=True)
    )
    bots = await request(plugin, ctx, ["Helperbot"])
    assert bots.is_error and bots.pending_action is None
    ctx.platform.members[group.key].discard(OWNER.key)
    denied = await request(plugin, ctx, list(names))
    assert denied.is_error and denied.pending_action is None


@pytest.mark.parametrize("mode", ["group", "admin", "delegate", "other_dm"])
async def test_non_owner_or_non_private_context_cannot_resolve_or_stage_people(mode):
    from dataclasses import replace

    from chatcore.skills import GateState

    plugin, ctx, names, group = await make_plugin()
    if mode == "group":
        ctx = replace(ctx, chat=group, is_dm=False)
    elif mode == "other_dm":
        ctx = replace(ctx, chat=ChatRef("tg", "3"))
    else:
        ctx = replace(ctx, user=User(FRIENDS[0], display_name="Alex"), chat=ChatRef("tg", "2"))
        if mode == "delegate":
            await plugin.store.apply(
                actor=OWNER,
                owners=OWNERS,
                expected_revision=0,
                change=AdminChange("grant", group, FRIENDS[0]),
            )
    gate = plugin.tool_gates()["manage_trusted_inviters"]
    assert (await gate.user(ctx)).state == GateState.NOT_ALLOWED
    result = await request(plugin, ctx, list(names))
    assert result.is_error and result.pending_action is None
    assert not (await plugin.store.snapshot()).trusted_inviters


async def test_legacy_registry_hides_owner_tool_from_admins_and_groups():
    from dataclasses import replace

    from chatcore.tools.registry import ToolRegistry

    plugin, ctx, _, group = await make_plugin()
    registry = ToolRegistry(plugin.tools())
    assert "manage_trusted_inviters" in {t.name for t in registry.specs(is_admin=True, ctx=ctx)}
    admin = replace(ctx, user=User(FRIENDS[0], display_name="Admin"), chat=ChatRef("tg", "2"))
    for hidden in (admin, replace(ctx, chat=group, is_dm=False)):
        assert "manage_trusted_inviters" not in {
            t.name for t in registry.specs(is_admin=True, ctx=hidden)
        }
        denied = await registry.call(
            "manage_trusted_inviters", {"operation": "trust", "people": ["Ben"]}, hidden
        )
        assert denied.is_error and denied.pending_action is None


@pytest.mark.parametrize("mode", ["off", "on", "shadow"])
async def test_natural_request_offers_one_five_person_confirmation_and_requires_owner_click(mode):
    from dataclasses import replace

    from chatcore.agent import AgentResponder, AgentSettings
    from chatcore.app import BotApp
    from chatcore.limits import Limits
    from chatcore.llm.types import Completion, Message, ToolCall
    from chatcore.skill_turn import SkillSettings
    from chatcore.skills import SkillsMode
    from chatcore.testing import ScriptedProvider, message, press

    plugin, ctx, names, _ = await make_plugin()
    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant(
                    "",
                    (
                        ToolCall(
                            "batch-1",
                            "manage_trusted_inviters",
                            {"operation": "trust", "people": list(names)},
                        ),
                    ),
                ),
                "tool_use",
            )
        ]
    )
    responder = AgentResponder(
        provider,
        system_prompt="Help the owner manage invitations.",
        settings=AgentSettings(
            skills=SkillSettings(mode=SkillsMode(mode), chats=frozenset({"*"}), router="keyword")
        ),
        limits=Limits(ctx.stores.cache),
    )
    app = BotApp(
        ctx.platform,
        stores=ctx.stores,
        access=plugin.access,
        plugins=[plugin],
        responder=responder,
        bot_name="testbot",
    )
    incoming = replace(
        message(
            "Let Alex, Ben, Chris, Dana and Eli invite you to groups.", dm=True, sender=ctx.user
        ),
        chat=ctx.chat,
    )
    from chatcore.skill_turn import effective_mode

    assert effective_mode(responder.settings.skills, incoming.chat) is SkillsMode(mode)
    await app.handle(incoming)
    assert "manage_trusted_inviters" in {tool.name for tool in provider.calls[0]["tools"]}
    prompts = [item for item in ctx.platform.sent if item.action_id]
    assert len(prompts) == 1
    assert not (await plugin.store.snapshot()).trusted_inviters
    prompt = prompts[0]
    await app.handle(
        press(prompt.action_id, chat=ctx.chat, sender=User(FRIENDS[0], display_name="Alex"))
    )
    assert not (await plugin.store.snapshot()).trusted_inviters
    await app.handle(press(prompt.action_id, chat=ctx.chat, sender=ctx.user))
    assert len((await plugin.store.snapshot()).trusted_inviters) == 5
    await app.handle(press(prompt.action_id, chat=ctx.chat, sender=ctx.user))
    assert (await plugin.store.snapshot()).revision == 1


@pytest.mark.parametrize("mode", ["off", "on", "shadow"])
async def test_natural_invitation_tool_is_absent_from_non_owner_admin_provider_requests(mode):
    from dataclasses import replace

    from chatcore.agent import AgentResponder, AgentSettings
    from chatcore.app import BotApp
    from chatcore.limits import Limits
    from chatcore.llm.types import Completion, Message
    from chatcore.skill_turn import SkillSettings
    from chatcore.skills import SkillsMode
    from chatcore.testing import ScriptedProvider, message

    plugin, ctx, _, _ = await make_plugin()
    plugin.access.admin_users = frozenset({FRIENDS[0].key})
    provider = ScriptedProvider(
        [Completion(Message.assistant("Owner access is required."), "end_turn")]
    )
    app = BotApp(
        ctx.platform,
        stores=ctx.stores,
        access=plugin.access,
        plugins=[plugin],
        responder=AgentResponder(
            provider,
            system_prompt="rules",
            settings=AgentSettings(
                skills=SkillSettings(
                    mode=SkillsMode(mode), chats=frozenset({"*"}), router="keyword"
                )
            ),
            limits=Limits(ctx.stores.cache),
        ),
        bot_name="testbot",
    )
    admin = User(FRIENDS[0], display_name="Alex")
    await app.handle(
        replace(
            message("Trust Ben to invite you to groups.", dm=True, sender=admin),
            chat=ChatRef("tg", admin.ref.id),
        )
    )
    assert "manage_trusted_inviters" not in {tool.name for tool in provider.calls[0]["tools"]}
    assert not (await plugin.store.snapshot()).trusted_inviters


async def test_twenty_long_unicode_names_fit_in_a_single_telegram_confirmation():
    plugin, ctx, _, group = await make_plugin()
    names = []
    for i in range(20, 40):
        name = "😀" * 70 + str(i)
        names.append(name)
        await ctx.stores.repo.upsert_member(group, User(UserRef("tg", str(i)), display_name=name))
    result = await request(plugin, ctx, names)
    assert result.pending_action
    assert len(result.content.encode("utf-16-le")) // 2 <= ctx.platform.capabilities.max_message_len


async def test_stale_batch_confirmation_changes_no_people():
    from chatcore.plugin import CommandContext

    plugin, ctx, names, group = await make_plugin()
    result = await request(plugin, ctx, list(names))
    await plugin.store.apply(
        actor=OWNER, owners=OWNERS, expected_revision=0, change=AdminChange("disable", group)
    )
    response = await plugin.commit(
        result.pending_action,
        CommandContext(ctx.chat, ctx.user, True, True, ctx.platform, ctx.stores),
    )
    assert "changed" in response.lower()
    assert not (await plugin.store.snapshot()).trusted_inviters


async def test_expired_batch_confirmation_cannot_be_claimed():
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from chatcore.app import BotApp
    from chatcore.testing import press

    plugin, ctx, names, _ = await make_plugin()
    result = await request(plugin, ctx, list(names))
    expired = replace(result.pending_action, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    await ctx.stores.repo.save_pending(expired)
    app = BotApp(
        ctx.platform, stores=ctx.stores, access=plugin.access, plugins=[plugin], bot_name="testbot"
    )
    await app.handle(press(expired.id, chat=ctx.chat, sender=ctx.user))
    assert not (await plugin.store.snapshot()).trusted_inviters


async def test_context_guard_fails_closed_without_context_or_when_callback_raises(caplog):
    from chatcore.tools.registry import Tool, ToolRegistry, ToolResult

    _, ctx, _, _ = await make_plugin()

    async def handler(ctx):
        return ToolResult("should not run")

    def broken(ctx):
        raise RuntimeError("private guard details")

    tool = Tool(
        "guarded",
        "Guarded test tool",
        {"type": "object", "properties": {}},
        handler,
        context_allowed=broken,
    )
    registry = ToolRegistry([tool])
    assert not registry.specs(is_admin=True)
    assert not registry.specs(is_admin=True, ctx=ctx)
    assert (await registry.call("guarded", {}, ctx)).is_error
    assert "private guard details" not in caplog.text
