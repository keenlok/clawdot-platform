from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.store import (
    AccessSnapshot,
    AdminChange,
    AuditEntry,
    MemoryAdministrationStore,
    Restriction,
    StaleChangeError,
    global_scope,
    is_global_scope,
    validate_change,
)
from chatcore.app import BotApp
from chatcore.display_time_store import display_service
from chatcore.events import ButtonPress, ChatRef, UserRef
from chatcore.limits import Limits, LimitsSettings
from chatcore.pending import PendingAction
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.testing import GROUP, FakePlatform, memory_stores, message, press, user


def test_global_scope_helpers_and_restriction_validation():
    scope = global_scope("tg")
    assert scope == ChatRef("tg", "*")
    change = AdminChange("block", scope, UserRef("tg", "42"))
    validate_change(change)
    with pytest.raises(ValueError):
        validate_change(AdminChange("enable", scope))
    with pytest.raises(ValueError):
        validate_change(AdminChange("pause", scope, UserRef("tg", "42"), 59))
    validate_change(AdminChange("pause", scope, UserRef("tg", "42"), 30 * 86400))
    with pytest.raises(ValueError):
        validate_change(AdminChange("pause", scope, UserRef("tg", "42"), 30 * 86400 + 1))
    with pytest.raises(ValueError):
        validate_change(AdminChange("limit", scope, UserRef("tg", "42"), True))
    with pytest.raises(ValueError):
        validate_change(AdminChange("limit", scope, UserRef("tg", "42"), 0))
    with pytest.raises(ValueError):
        validate_change(AdminChange("limit", scope, UserRef("tg", "42"), 10001))
    validate_change(AdminChange("limit", scope, UserRef("tg", "42"), 10000))
    validate_change(AdminChange("limit_reset", scope, UserRef("tg", "42")))
    for operation in ("enable", "disable", "grant", "moderator_grant", "persona_reset"):
        with pytest.raises(ValueError):
            validate_change(AdminChange(operation, scope))
    with pytest.raises(ValueError):
        validate_change(AdminChange("block", ChatRef("tg", "-1"), UserRef("console", "42")))
    with pytest.raises(ValueError):
        validate_change(AdminChange("limit", ChatRef("tg", "-1"), UserRef("tg", "42"), 10))
    assert is_global_scope(scope)


@pytest.mark.parametrize(
    "change",
    [
        AdminChange("block", ChatRef("console", "group"), UserRef("console", "*")),
        AdminChange("moderator_grant", ChatRef("console", "group"), UserRef("console", "*")),
        AdminChange("limit", global_scope("console"), UserRef("console", "*"), 5),
        AdminChange("moderator_grant", ChatRef("console", "*"), UserRef("console", "42")),
    ],
)
def test_non_telegram_sentinel_is_not_a_valid_group_or_user_target(change):
    with pytest.raises(ValueError):
        validate_change(change)


@pytest.mark.asyncio
async def test_store_restrictions_moderation_targets_and_revision():
    owner, moderator, target = (UserRef("tg", key) for key in ("1", "2", "3"))
    group = ChatRef("tg", "-100")
    store = MemoryAdministrationStore()
    owners = frozenset({owner.key})
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("moderator_grant", group, moderator),
    )
    await store.apply(
        actor=moderator,
        owners=owners,
        expected_revision=1,
        change=AdminChange("pause", group, target, 3600),
    )
    snapshot = await store.snapshot()
    assert snapshot.revision == 2
    assert (moderator.key, group.key) in snapshot.moderators
    assert snapshot.restrictions[(target.key, group.key)].kind == "pause"
    with pytest.raises(PermissionError):
        await store.apply(
            actor=moderator,
            owners=owners,
            expected_revision=2,
            change=AdminChange("block", global_scope("tg"), target),
        )
    with pytest.raises(ValueError):
        await store.apply(
            actor=moderator,
            owners=owners,
            expected_revision=2,
            change=AdminChange("block", group, moderator),
        )


@pytest.mark.asyncio
async def test_moderation_is_scoped_and_targets_are_protected():
    owner, moderator, target, other_mod, full_admin = (
        UserRef("tg", key) for key in ("1", "2", "3", "4", "5")
    )
    setup_admin = UserRef("tg", "6")
    group, other = ChatRef("tg", "-100"), ChatRef("tg", "-200")
    owners = frozenset({owner.key})
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("moderator_grant", group, moderator),
    )
    with pytest.raises(PermissionError):
        await store.apply(
            actor=moderator,
            owners=owners,
            expected_revision=1,
            change=AdminChange("block", other, target),
        )
    with pytest.raises(PermissionError):
        await store.apply(
            actor=moderator,
            owners=owners,
            expected_revision=1,
            change=AdminChange("block", global_scope("tg"), target),
        )
    with pytest.raises(PermissionError):
        await store.apply(
            actor=moderator,
            owners=owners,
            expected_revision=1,
            change=AdminChange("enable", group),
        )
    for target_ref in (moderator, owner):
        with pytest.raises(ValueError):
            await store.apply(
                actor=moderator,
                owners=owners,
                expected_revision=1,
                change=AdminChange("block", group, target_ref),
            )
    with pytest.raises(ValueError):
        await store.apply(
            actor=moderator,
            owners=owners,
            protected=frozenset({full_admin.key}),
            expected_revision=1,
            change=AdminChange("block", group, full_admin),
        )
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=1,
        change=AdminChange("moderator_grant", group, other_mod),
    )
    with pytest.raises(ValueError):
        await store.apply(
            actor=moderator,
            owners=owners,
            expected_revision=2,
            change=AdminChange("block", group, other_mod),
        )
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=2,
        change=AdminChange("grant", group, setup_admin),
    )
    with pytest.raises(PermissionError):
        await store.apply(
            actor=setup_admin,
            owners=owners,
            expected_revision=3,
            change=AdminChange("block", group, target),
        )
    for op in ("moderator_grant", "moderator_revoke", "limit"):
        with pytest.raises(PermissionError):
            await store.apply(
                actor=moderator,
                owners=owners,
                expected_revision=3,
                change=AdminChange(
                    op,
                    global_scope("tg") if op == "limit" else group,
                    target,
                    10 if op == "limit" else None,
                ),
            )


@pytest.mark.asyncio
async def test_each_new_operation_revises_and_audits_global_scope_and_machine_detail():
    owner, target = UserRef("tg", "1"), UserRef("tg", "2")
    group = ChatRef("tg", "-100")
    owners = frozenset({owner.key})
    store = MemoryAdministrationStore()
    changes = [
        AdminChange("moderator_grant", group, target),
        AdminChange("pause", global_scope("tg"), target, 600),
        AdminChange("unblock", global_scope("tg"), target),
        AdminChange("limit", global_scope("tg"), target, 50),
        AdminChange("limit_reset", global_scope("tg"), target),
    ]
    for revision, change in enumerate(changes):
        await store.apply(actor=owner, owners=owners, expected_revision=revision, change=change)
    snapshot = await store.snapshot()
    assert snapshot.revision == len(changes)
    assert target.key not in snapshot.user_limits
    rows = await store.audit()
    assert [row.operation for row in rows] == [
        "limit_reset",
        "limit",
        "unblock",
        "pause",
        "moderator_grant",
    ]
    assert rows[1].detail == "limit=50"
    assert rows[3].detail.startswith("until=")
    assert rows[0].group == "tg:*"
    assert [row.operation for row in await store.audit(groups=frozenset({group.key}))] == [
        "moderator_grant"
    ]
    with pytest.raises(StaleChangeError):
        await store.apply(
            actor=owner,
            owners=owners,
            expected_revision=0,
            change=AdminChange("block", group, target),
        )


def test_access_policy_applies_global_group_expired_and_owner_rules():
    from chatcore.administration.store import AccessSnapshot, Restriction

    now = datetime.now(UTC)
    user = UserRef("tg", "42")
    group = ChatRef("tg", "-100")
    owner = UserRef("tg", "1")
    snapshot = AccessSnapshot(
        1,
        {},
        frozenset(),
        restrictions={
            (user.key, "tg:*"): Restriction(user.key, "tg:*", "block", None, owner.key, now),
            (user.key, group.key): Restriction(
                user.key, group.key, "pause", now + timedelta(hours=1), owner.key, now
            ),
        },
    )
    policy = AccessPolicy([], [], owners=[owner.key])
    policy._snapshot = snapshot
    assert policy.restriction_for(user, group, is_dm=True, now=now).kind == "block"
    assert policy.restriction_for(user, group, is_dm=False, now=now).kind == "block"
    assert policy.restriction_for(owner, group, is_dm=False, now=now) is None
    assert (
        policy.restriction_for(
            UserRef("console", "42"), ChatRef("console", "room"), is_dm=False, now=now
        )
        is None
    )
    assert policy.user_limit(user) is None


def test_access_policy_group_scope_expiry_precedence_limits_and_platform_isolation():
    from chatcore.administration.store import AccessSnapshot, Restriction

    now = datetime.now(UTC)
    person, console = UserRef("tg", "42"), UserRef("console", "42")
    group = ChatRef("tg", "-100")
    owner = UserRef("tg", "1")
    snapshot = AccessSnapshot(
        4,
        {},
        frozenset(),
        restrictions={
            (person.key, group.key): Restriction(
                person.key, group.key, "pause", now + timedelta(hours=1), owner.key, now
            ),
            (person.key, "tg:*-bad"): Restriction(
                person.key, "tg:*-bad", "block", None, owner.key, now
            ),
            (console.key, "console:*"): Restriction(
                console.key, "console:*", "block", None, owner.key, now
            ),
        },
        user_limits={person.key: 1},
    )
    policy = AccessPolicy([], [], owners=[owner.key])
    policy._snapshot = snapshot
    assert policy.restriction_for(person, group, is_dm=True, now=now) is None
    assert policy.restriction_for(person, group, is_dm=False, now=now).kind == "pause"
    expired = replace(
        snapshot.restrictions[(person.key, group.key)], until=now - timedelta(seconds=1)
    )
    policy._snapshot = replace(
        snapshot,
        restrictions={**snapshot.restrictions, (person.key, group.key): expired},
    )
    assert policy.restriction_for(person, group, is_dm=False, now=now) is None
    assert policy.user_limit(person) == 1
    assert policy.restriction_for(console, ChatRef("console", "room"), is_dm=False).kind == "block"


class CountingResponder:
    def __init__(self):
        self.calls = 0

    async def respond(self, message, text, app):
        self.calls += 1

    async def consider(self, message, app):
        self.calls += 1


async def test_blocked_group_message_is_logged_not_answered_and_notice_is_cooled_down():
    owner, target = UserRef("fake", "owner"), user("alice").ref
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("block", GROUP, target),
    )
    platform = FakePlatform()
    responder = CountingResponder()
    access = AccessPolicy([GROUP.key], [], owners=[owner.key], store=store)
    app = BotApp(platform, stores=stores, access=access, responder=responder, bot_name="sbbot")
    await app.handle(message("@sbbot hi", mention=True, sender=user("alice")))
    await app.handle(message("@sbbot again", mention=True, sender=user("alice")))
    assert platform.texts == ["You can't use this bot here right now."]
    assert responder.calls == 0
    logged = await stores.repo.recent_messages(GROUP, limit=10)
    assert len([row for row in logged if not row.is_bot]) == 2


async def test_expired_pause_allows_next_message():
    from dataclasses import replace

    owner, target = UserRef("fake", "owner"), user("alice").ref
    stores = memory_stores()
    await display_service(stores).set_zone(
        GROUP, user("alice").ref, is_dm=False, zone="Asia/Singapore"
    )
    store = MemoryAdministrationStore(stores.repo)
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("pause", GROUP, target, 3600),
    )
    platform = FakePlatform()
    access = AccessPolicy([GROUP.key], [], owners=[owner.key], store=store)
    app = BotApp(platform, stores=stores, access=access, bot_name="sbbot")
    await app.handle(message("@sbbot hello", mention=True, sender=user("alice")))
    row = store._restrictions[(target.key, GROUP.key)]
    formatted = row.until.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Singapore")).strftime(
        "%Y-%m-%d %H:%M:%S Asia/Singapore (UTC+08:00)"
    )
    assert platform.texts == [f"Your access to this bot is paused until {formatted}."]
    store._restrictions[(target.key, GROUP.key)] = replace(
        row, until=datetime.now(UTC) - timedelta(seconds=1)
    )
    await app.handle(message("@sbbot hello again", mention=True, sender=user("alice")))
    assert platform.texts[-1] == "echo: hello again"


class PrivacyTestPlugin(BasePlugin):
    name = "privacy"

    def __init__(self):
        self.commits = 0
        self.commands_called = 0

    def commands(self):
        async def handler(ctx: CommandContext, _args: str):
            self.commands_called += 1
            return "privacy reply"

        return [
            Command("privacy", "privacy help", handler),
            Command("forgetme", "delete self", handler),
        ]

    def action_handlers(self):
        return {"forgetme": self}

    async def commit(self, action, ctx):
        self.commits += 1
        return "privacy action saved"


class OrdinaryActionPlugin(BasePlugin):
    name = "ledger"

    def __init__(self):
        self.commits = 0

    def action_handlers(self):
        return {"add": self}

    async def commit(self, action, ctx):
        self.commits += 1
        return "saved"


@pytest.mark.parametrize("buttons", [True, False])
async def test_global_block_allows_dm_privacy_commands_and_forgetme_confirmation(buttons):
    from dataclasses import replace

    owner, person = UserRef("fake", "owner"), user("alice")
    dm = ChatRef("fake", person.ref.id)
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    plugin = PrivacyTestPlugin()
    base_platform = FakePlatform()
    platform = FakePlatform(capabilities=replace(base_platform.capabilities, buttons=buttons))
    # Preserve an independent DM grant so this test exercises the privacy exemptions,
    # rather than the access admission check.
    access = AccessPolicy([], [person.ref.key], owners=[owner.key], store=store)
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[plugin],
        bot_name="sbbot",
        restriction_exempt_commands=frozenset({"privacy", "forgetme"}),
        restriction_exempt_actions=frozenset({"privacy.forgetme"}),
    )
    action = PendingAction.create(
        chat=dm,
        requester=person.ref,
        plugin="privacy",
        kind="forgetme",
        payload={},
        summary="confirm",
    )
    await app.confirm.request(action, person)
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("block", global_scope("fake"), person.ref),
    )
    await app.handle(message("/privacy", dm=True, sender=person))
    await app.handle(message("/forgetme", dm=True, sender=person))
    assert platform.texts[-2:] == ["privacy reply", "privacy reply"]
    if buttons:
        await app.handle(ButtonPress(dm, person, action.id, "confirm"))
    else:
        code = action.reply_code
        await app.handle(message(f"YES {code}", dm=True, sender=person))
    assert plugin.commits == 1
    await app.handle(message("/help", dm=True, sender=person))
    assert platform.texts[-1] == "You can't use this bot here right now."
    count = len(platform.sent)
    await app.handle(message("normal DM text", dm=True, sender=person))
    assert len(platform.sent) == count


async def test_group_block_does_not_exempt_forgetme_command():
    owner, person = user("owner"), user("alice")
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    await store.apply(
        actor=owner.ref,
        owners=frozenset({owner.ref.key}),
        expected_revision=0,
        change=AdminChange("block", GROUP, person.ref),
    )
    access = AccessPolicy([GROUP.key], [], owners=[owner.ref.key], store=store)
    plugin = PrivacyTestPlugin()
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[plugin],
        bot_name="sbbot",
        restriction_exempt_commands=frozenset({"privacy", "forgetme"}),
    )
    await app.handle(message("/forgetme@sbbot", sender=person))
    assert plugin.commands_called == 0
    assert platform.texts == ["You can't use this bot here right now."]


async def test_owner_remains_unrestricted_when_snapshot_has_stale_restriction():
    from chatcore.administration.store import Restriction

    owner = user("owner")
    now = datetime.now(UTC)
    store = MemoryAdministrationStore()
    store._restrictions[(owner.ref.key, "fake:*")] = Restriction(
        owner.ref.key, "fake:*", "block", None, "fake:old-admin", now
    )
    access = AccessPolicy([GROUP.key], [], owners=[owner.ref.key], store=store)
    platform = FakePlatform()
    app = BotApp(platform, stores=memory_stores(), access=access, bot_name="sbbot")
    await app.handle(message("@sbbot hello", mention=True, sender=owner))
    assert platform.texts == ["echo: hello"]


async def test_globally_blocked_nonmember_cannot_start_a_dm_or_receive_notice():
    owner, person = UserRef("fake", "owner"), user("outsider")
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("block", global_scope("fake"), person.ref),
    )
    platform = FakePlatform()
    access = AccessPolicy([GROUP.key], [], owners=[owner.key], store=store)
    app = BotApp(platform, stores=stores, access=access, bot_name="sbbot")
    await app.handle(message("hello", dm=True, sender=person))
    assert platform.sent == []


async def test_restricted_admin_notice_obeys_group_routing():
    from chatcore.administration.plugin import AdministrationPlugin

    owner, person, bot_person = user("owner"), user("alice"), user("bot-user", is_bot=True)
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    await store.apply(
        actor=owner.ref,
        owners=frozenset({owner.ref.key}),
        expected_revision=0,
        change=AdminChange("block", GROUP, person.ref),
    )
    await store.apply(
        actor=owner.ref,
        owners=frozenset({owner.ref.key}),
        expected_revision=1,
        change=AdminChange("block", GROUP, bot_person.ref),
    )
    access = AccessPolicy([GROUP.key], [], owners=[owner.ref.key], store=store)
    platform = FakePlatform()
    plugin = AdministrationPlugin(access, store, frozenset({owner.ref.key}))
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[plugin],
        bot_name="bot",
        group_mentions_only=True,
    )
    await app.handle(message("/admin", sender=person))
    assert platform.sent == []
    await app.handle(message("/admin@bot", sender=bot_person, mention=True))
    assert platform.sent == []
    await app.handle(message("/admin@bot", sender=person, mention=True))
    assert platform.texts == ["You can't use this bot here right now."]


async def test_admin_persona_confirmation_preserves_internal_whitespace_and_newlines():
    from chatcore.administration.plugin import AdministrationPlugin

    owner = user("owner")
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    access = AccessPolicy([GROUP.key], [], owners=[owner.ref.key], store=store)
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[AdministrationPlugin(access, store, frozenset({owner.ref.key}))],
        bot_name="bot",
    )
    persona = "First  line\nSecond line\n  Third line"
    await app.handle(message(f"/admin persona {GROUP.key} {persona}", dm=True, sender=owner))
    pending = await stores.repo.get_pending(
        ChatRef("fake", owner.ref.id), platform.sent[-1].action_id
    )
    assert pending.payload["value"] == persona
    await app.handle(press(pending.id, sender=owner, chat=pending.chat))
    assert (await stores.repo.get_chat_settings(GROUP)).persona == persona


@pytest.mark.parametrize("buttons", [True, False])
async def test_global_block_refuses_preexisting_nonexempt_pending_confirmation(buttons):
    from dataclasses import replace

    owner, person = UserRef("fake", "owner"), user("alice")
    dm = ChatRef("fake", person.ref.id)
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    plugin = OrdinaryActionPlugin()
    original = FakePlatform()
    platform = FakePlatform(capabilities=replace(original.capabilities, buttons=buttons))
    access = AccessPolicy([], [person.ref.key], owners=[owner.key], store=store)
    app = BotApp(platform, stores=stores, access=access, plugins=[plugin], bot_name="sbbot")
    action = PendingAction.create(
        chat=dm, requester=person.ref, plugin="ledger", kind="add", payload={}, summary="confirm"
    )
    await app.confirm.request(action, person)
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("block", global_scope("fake"), person.ref),
    )
    if buttons:
        await app.handle(ButtonPress(dm, person, action.id, "confirm"))
    else:
        await app.handle(message(f"YES {action.reply_code}", dm=True, sender=person))
    assert plugin.commits == 0
    assert platform.texts[-1] == "You can't use this bot here right now."


async def test_event_boundary_drops_global_sentinel_before_store_or_platform_access():
    from chatcore.events import MemberJoined

    class RecordingRepo:
        def __init__(self):
            self.reads = self.writes = 0

        def __getattr__(self, name):
            async def record(*args, **kwargs):
                if name.startswith(("get_", "find_", "recent_")):
                    self.reads += 1
                else:
                    self.writes += 1

            return record

    repo = RecordingRepo()
    stores = memory_stores()
    stores.repo = repo
    platform = FakePlatform()
    responder = CountingResponder()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        responder=responder,
        bot_name="sbbot",
    )
    for chat, sender in ((ChatRef("tg", "*"), user("alice")), (GROUP, user("*"))):
        await app.handle(message("@sbbot no", chat=chat, sender=sender, mention=True))
        await app.handle(ButtonPress(chat, sender, "action", "confirm"))
        await app.handle(MemberJoined(chat, sender))
    assert repo.reads == repo.writes == 0
    assert platform.sent == []
    assert responder.calls == 0


async def test_user_limit_override_and_default_fallback():
    from chatcore.testing import memory_stores

    stores = memory_stores()
    alice = UserRef("fake", "alice")
    bob = UserRef("fake", "bob")
    limits = Limits(
        stores.cache,
        LimitsSettings(RATE_USER_PER_HOUR=20),
        user_limit=lambda ref: 1 if ref == alice else None,
    )
    assert await limits.check(GROUP, alice) is None
    assert (
        await limits.check(GROUP, alice)
        == "You've reached the hourly request limit. Please try again later."
    )
    assert await limits.check(GROUP, bob) is None
    assert await limits.check(GROUP, bob) is None

    owner = UserRef("fake", "owner")
    admin = MemoryAdministrationStore()
    await admin.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("limit", global_scope("fake"), alice, 1),
    )
    policy = AccessPolicy([], [], owners=[owner.key], store=admin)
    await policy.refresh()
    assert policy.user_limit(alice) == 1
    await admin.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=1,
        change=AdminChange("limit_reset", global_scope("fake"), alice),
    )
    await policy.refresh()
    assert policy.user_limit(alice) is None


async def test_admin_moderation_commands_confirm_and_moderator_menu_is_scoped():
    from chatcore.administration.plugin import AdministrationPlugin

    owner, moderator, target = (user(name) for name in ("owner", "moderator", "target"))
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    access = AccessPolicy([GROUP.key], [], owners=[owner.ref.key], store=store)
    platform = FakePlatform()
    plugin = AdministrationPlugin(access, store, frozenset({owner.ref.key}))
    app = BotApp(platform, stores=stores, access=access, plugins=[plugin], bot_name="sbbot")

    await app.handle(message("/admin pause fake:target 2h", dm=True, sender=owner))
    pending = await stores.repo.get_pending(
        ChatRef("fake", owner.ref.id), platform.sent[-1].action_id
    )
    assert pending.summary == "Confirm: pause fake:target everywhere (global) for 2h."
    await app.handle(press(pending.id, sender=owner, chat=ChatRef("fake", owner.ref.id)))
    assert (await store.snapshot()).restrictions[(target.ref.key, "fake:*")].kind == "pause"

    await app.handle(message("/admin mod fake:moderator fake:group", dm=True, sender=owner))
    mod_action = await stores.repo.get_pending(
        ChatRef("fake", owner.ref.id), platform.sent[-1].action_id
    )
    assert mod_action.summary == "Confirm: grant fake:moderator moderation of fake:group."
    await app.handle(press(mod_action.id, sender=owner, chat=mod_action.chat))

    await app.handle(message("/admin", dm=True, sender=moderator))
    menu = platform.texts[-1]
    assert "/admin block USER_ID" in menu
    assert "/admin enable" not in menu
    await app.handle(message("/admin block fake:target fake:group", dm=True, sender=moderator))
    group_action = await stores.repo.get_pending(
        ChatRef("fake", moderator.ref.id), platform.sent[-1].action_id
    )
    assert group_action.summary == "Confirm: block fake:target in fake:group."
    await app.handle(press(group_action.id, sender=moderator, chat=group_action.chat))
    assert (await store.snapshot()).restrictions[(target.ref.key, GROUP.key)].kind == "block"

    sent_count = len(platform.sent)
    await app.handle(message("/admin limit fake:target 50", dm=True, sender=owner))
    assert len(platform.sent) == sent_count + 1
    limit_action = await stores.repo.get_pending(
        ChatRef("fake", owner.ref.id), platform.sent[-1].action_id
    )
    assert limit_action.summary == "Confirm: set fake:target hourly request limit to 50."
    await app.handle(press(limit_action.id, sender=owner, chat=limit_action.chat))
    assert (await store.snapshot()).user_limits[target.ref.key] == 50

    await app.handle(message("/admin restrictions", dm=True, sender=owner))
    assert "global pause" in platform.texts[-1]
    assert "fake:target limit 50/hour" in platform.texts[-1]
    await app.handle(message("/admin limit fake:target default", dm=True, sender=owner))
    reset_action = await stores.repo.get_pending(
        ChatRef("fake", owner.ref.id), platform.sent[-1].action_id
    )
    assert reset_action.summary == "Confirm: reset fake:target hourly request limit to the default."
    await app.handle(press(reset_action.id, sender=owner, chat=reset_action.chat))
    assert target.ref.key not in (await store.snapshot()).user_limits


@pytest.mark.parametrize(
    ("until", "expected"),
    [
        (datetime(2027, 7, 1, 13, tzinfo=UTC), "2027-07-01 14:00:00 Europe/London (UTC+01:00)"),
        (datetime(2027, 1, 1, 13, tzinfo=UTC), "2027-01-01 13:00:00 Europe/London (UTC+00:00)"),
    ],
)
async def test_admin_restrictions_and_pause_audit_render_instants_in_requester_dm_zone(
    until, expected, monkeypatch
):
    import chatcore.administration.plugin as admin_plugin
    from chatcore.administration.plugin import AdministrationPlugin

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 1, 1, tzinfo=UTC)
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(admin_plugin, "datetime", FrozenDateTime)

    owner, target = user("owner"), user("target")
    stores = memory_stores()

    class FixedAdminStore:
        async def snapshot(self):
            return AccessSnapshot(
                1,
                {},
                frozenset(),
                restrictions={
                    (target.ref.key, GROUP.key): Restriction(
                        target.ref.key, GROUP.key, "pause", until, owner.ref.key, until
                    )
                },
            )

        async def audit(self, *, groups=None, limit=20):
            return (
                AuditEntry(
                    1,
                    owner.ref.key,
                    "pause",
                    GROUP.key,
                    target.ref.key,
                    until,
                    f"until={until.isoformat().replace('+00:00', 'Z')}",
                ),
            )

    store = FixedAdminStore()
    await display_service(stores).set_zone(
        ChatRef("fake", owner.ref.id), owner.ref, is_dm=True, zone="Europe/London"
    )
    access = AccessPolicy([GROUP.key], [], owners=[owner.ref.key], store=store)
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=access,
        plugins=[AdministrationPlugin(access, store, frozenset({owner.ref.key}))],
        bot_name="sbbot",
    )

    await app.handle(message("/admin restrictions", dm=True, sender=owner))
    assert expected in app.platform.texts[-1]

    await app.handle(message("/admin audit", dm=True, sender=owner))
    assert f"until={expected}" in app.platform.texts[-1]


def test_audit_detail_only_formats_known_pause_until_field():
    from chatcore.administration.plugin import _audit_display_detail

    assert _audit_display_detail("limit", "until=2026-01-01T00:00:00Z", "UTC") == (
        "until=2026-01-01T00:00:00Z"
    )
    assert _audit_display_detail("pause", "arbitrary 2026-01-01T00:00:00Z text", "UTC") == (
        "arbitrary 2026-01-01T00:00:00Z text"
    )


async def test_unmod_and_group_and_global_unblock_commands_confirm_and_commit():
    from chatcore.administration.plugin import AdministrationPlugin

    owner, moderator, target = (user(name) for name in ("owner", "moderator", "target"))
    owners = frozenset({owner.ref.key})
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner.ref,
        owners=owners,
        expected_revision=0,
        change=AdminChange("moderator_grant", GROUP, moderator.ref),
    )
    await store.apply(
        actor=owner.ref,
        owners=owners,
        expected_revision=1,
        change=AdminChange("block", GROUP, target.ref),
    )
    await store.apply(
        actor=owner.ref,
        owners=owners,
        expected_revision=2,
        change=AdminChange("block", global_scope("fake"), target.ref),
    )
    access = AccessPolicy([GROUP.key], [], owners=owners, store=store)
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[AdministrationPlugin(access, store, owners)],
        bot_name="sbbot",
    )

    await app.handle(message("/admin unblock fake:target fake:group", dm=True, sender=moderator))
    group_unblock = await app.stores.repo.get_pending(
        ChatRef("fake", moderator.ref.id), platform.sent[-1].action_id
    )
    assert group_unblock.summary == "Confirm: unblock fake:target in fake:group."
    await app.handle(press(group_unblock.id, sender=moderator, chat=group_unblock.chat))
    assert (target.ref.key, GROUP.key) not in (await store.snapshot()).restrictions

    await app.handle(message("/admin unmod fake:moderator fake:group", dm=True, sender=owner))
    unmod = await app.stores.repo.get_pending(
        ChatRef("fake", owner.ref.id), platform.sent[-1].action_id
    )
    assert unmod.summary == "Confirm: revoke fake:moderator's moderation of fake:group."
    await app.handle(press(unmod.id, sender=owner, chat=unmod.chat))
    assert (moderator.ref.key, GROUP.key) not in (await store.snapshot()).moderators

    await app.handle(message("/admin unblock fake:target", dm=True, sender=owner))
    global_unblock = await app.stores.repo.get_pending(
        ChatRef("fake", owner.ref.id), platform.sent[-1].action_id
    )
    assert global_unblock.summary == "Confirm: unblock fake:target everywhere (global)."
    await app.handle(press(global_unblock.id, sender=owner, chat=global_unblock.chat))
    assert (target.ref.key, "fake:*") not in (await store.snapshot()).restrictions


@pytest.mark.parametrize(
    "operation,value",
    [("limit", [50]), ("unknown", None)],
)
async def test_admin_commit_rejects_tampered_value_or_unknown_operation_without_revision_change(
    operation, value
):
    from chatcore.administration.plugin import AdministrationPlugin
    from chatcore.pending import PendingAction

    owner, target = user("owner"), user("target")
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    owners = frozenset({owner.ref.key})
    access = AccessPolicy([], [], owners=owners, store=store)
    platform = FakePlatform()
    plugin = AdministrationPlugin(access, store, owners)
    app = BotApp(platform, stores=stores, access=access, plugins=[plugin], bot_name="sbbot")
    chat = ChatRef("fake", owner.ref.id)
    action = PendingAction.create(
        chat=chat,
        requester=owner.ref,
        plugin="administration",
        kind="change",
        payload={
            "operation": operation,
            "group": "fake:*" if operation == "limit" else GROUP.key,
            "user": target.ref.key,
            "value": value,
            "expected_revision": 0,
        },
        summary="tampered change",
    )
    ctx = app.command_context(chat, owner, is_dm=True)

    result = await plugin._commit(action, ctx)

    assert result == "That confirmation is invalid. Start the command again."
    snapshot = await store.snapshot()
    assert snapshot.revision == 0
    assert snapshot.user_limits == {}
    assert snapshot.restrictions == {}
    assert snapshot.moderators == frozenset()


async def test_protected_targets_are_refused_before_a_confirmation_is_created():
    from chatcore.administration.plugin import AdministrationPlugin

    owner, moderator, admin = (user(name) for name in ("owner", "moderator", "admin"))
    owners = frozenset({owner.ref.key})
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    await store.apply(
        actor=owner.ref,
        owners=owners,
        expected_revision=0,
        change=AdminChange("moderator_grant", GROUP, moderator.ref),
    )
    access = AccessPolicy([GROUP.key], [admin.ref.key], owners=owners, store=store)
    platform = FakePlatform()
    plugin = AdministrationPlugin(access, store, owners, protected=frozenset({admin.ref.key}))
    app = BotApp(platform, stores=stores, access=access, plugins=[plugin], bot_name="sbbot")

    await app.handle(message("/admin block fake:owner", dm=True, sender=owner))
    await app.handle(message("/admin block fake:admin fake:group", dm=True, sender=moderator))
    await app.handle(
        message("/admin pause fake:moderator 1h fake:group", dm=True, sender=moderator)
    )

    assert platform.texts[-3:] == ["That person can't be the target of this change."] * 3
    assert not [sent for sent in platform.sent if getattr(sent, "action_id", None)]
    assert (await store.snapshot()).revision == 1


async def test_global_save_reply_does_not_expose_the_scope_sentinel():
    from chatcore.administration.plugin import AdministrationPlugin

    owner = user("owner")
    owners = frozenset({owner.ref.key})
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    access = AccessPolicy([GROUP.key], [], owners=owners, store=store)
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[AdministrationPlugin(access, store, owners)],
        bot_name="sbbot",
    )
    await app.handle(message("/admin block fake:bob", dm=True, sender=owner))
    action_id = platform.sent[-1].action_id
    await app.handle(press(action_id, sender=owner, chat=ChatRef("fake", owner.ref.id)))
    assert platform.texts[-1] == "Saved block everywhere."
