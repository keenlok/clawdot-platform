"""Event-boundary access refresh behavior."""

from dataclasses import replace

import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.store import AdminChange, DmGrant, MemoryAdministrationStore
from chatcore.app import BotApp
from chatcore.events import ChatRef, User, UserRef
from chatcore.plugin import BasePlugin
from chatcore.testing import GROUP, FakePlatform, memory_stores, message, press, user


def build_dm_admin_app(*, admin_users=(), delegate=None):
    from chatcore.administration.plugin import AdministrationPlugin

    owner = user("owner")
    store = MemoryAdministrationStore()
    owners = frozenset({owner.ref.key})
    if delegate is not None:
        # The caller's delegate identity is linked to the allowed group below.
        store._assignments.add((delegate.ref.key, GROUP.key))
    stores = memory_stores()
    platform = FakePlatform()
    access = AccessPolicy([GROUP.key], admin_users, owners=owners, store=store)
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[AdministrationPlugin(access, store, owners)],
        bot_name="sbbot",
    )
    return owner, store, stores, platform, access, app


async def test_admin_group_commands_respect_mentions_only_while_dm_setup_still_works():
    from chatcore.administration.plugin import AdministrationPlugin

    owner = user("owner")
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    platform = FakePlatform()
    access = AccessPolicy([GROUP.key], [], owners=[owner.ref.key], store=store)
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[AdministrationPlugin(access, store, frozenset({owner.ref.key}))],
        bot_name="sbbot",
        group_mentions_only=True,
    )

    await app.handle(message("/admin groups", sender=owner))
    assert platform.sent == []
    await app.handle(message("/admin@sbbot groups", sender=owner))
    assert "private chat" in platform.texts[-1]
    await app.handle(message("/admin groups", sender=owner, dm=True))
    assert GROUP.key in platform.texts[-1]


@pytest.mark.asyncio
async def test_newly_disabled_bootstrap_group_is_not_logged_or_answered():
    owner = UserRef("fake", "owner")
    store = MemoryAdministrationStore()
    access = AccessPolicy([GROUP.key], [], owners=[owner.key], store=store)
    app = BotApp(FakePlatform(), stores=memory_stores(), access=access, bot_name="sbbot")
    await store.apply(
        actor=owner,
        owners=frozenset({owner.key}),
        expected_revision=0,
        change=AdminChange("disable", GROUP),
    )
    await app.handle(message("@sbbot hello", mention=True))
    assert app.platform.left == [GROUP]
    logged = await app.stores.repo.recent_messages(GROUP, limit=10)
    assert all(item.is_bot for item in logged)
    assert app.platform.texts == []


@pytest.mark.asyncio
async def test_refresh_failure_prevents_any_event_side_effect():
    class BrokenStore:
        async def snapshot(self):
            raise OSError("database unavailable")

    stores = memory_stores()
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], [], store=BrokenStore()),
        bot_name="sbbot",
    )
    await app.handle(message("@sbbot never process this", mention=True, sender=user("alice")))
    assert await stores.repo.recent_messages(GROUP, limit=10) == []
    assert platform.left == []
    assert platform.texts == []


async def test_dm_request_notice_is_off_by_default_and_rate_limited_without_logging():
    stores = memory_stores()
    platform = FakePlatform()
    app = BotApp(platform, stores=stores, access=AccessPolicy([], []), bot_name="sbbot")
    denied = message("hello", dm=True, sender=user("stranger"))
    await app.handle(denied)
    assert platform.texts == []
    assert await stores.repo.recent_messages(denied.chat, limit=10) == []

    class ProbePlugin(BasePlugin):
        name = "probe"

        def __init__(self):
            self.messages = 0

        async def on_message(self, incoming, stores):
            self.messages += 1

    class ProbeResponder:
        def __init__(self):
            self.calls = 0

        async def respond(self, incoming, text, app):
            self.calls += 1

    plugin, responder = ProbePlugin(), ProbeResponder()
    logged = 0
    original_log = stores.repo.log_message

    async def log_message(incoming):
        nonlocal logged
        logged += 1
        await original_log(incoming)

    stores.repo.log_message = log_message
    enabled = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([], []),
        bot_name="sbbot",
        dm_request_notice=True,
        plugins=[plugin],
        responder=responder,
    )
    await enabled.handle(denied)
    await enabled.handle(denied)
    await enabled.handle(message("hello", dm=True, sender=user("blocked-bot", is_bot=True)))
    assert len(platform.texts) == 1
    assert platform.texts[0] == (
        "I only chat with people the owner has approved. To ask for access, "
        "send the owner this ID: fake:stranger"
    )
    assert await stores.repo.recent_messages(denied.chat, limit=10) == []
    assert logged == plugin.messages == responder.calls == 0


async def test_dm_grant_does_not_create_group_membership_or_invitation_authority():
    from chatcore.administration.store import global_scope
    from chatcore.events import BotAddedToChat

    owner, grantee, trusted = user("owner"), user("grantee"), user("trusted")
    group, new_group = ChatRef("fake", "known"), ChatRef("fake", "new")
    store = MemoryAdministrationStore()
    owners = frozenset({owner.ref.key})
    await store.apply(
        actor=owner.ref,
        owners=owners,
        expected_revision=0,
        change=AdminChange("dm_grant", global_scope("fake"), grantee.ref),
    )
    await store.apply(
        actor=owner.ref,
        owners=owners,
        expected_revision=1,
        change=AdminChange("trust", global_scope("fake"), trusted.ref),
    )
    stores, platform = memory_stores(), FakePlatform()
    access = AccessPolicy([group.key], [], owners=owners, store=store)
    from chatcore.administration.plugin import AdministrationPlugin

    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[AdministrationPlugin(access, store, owners)],
        bot_name="sbbot",
    )
    assert await access.member_groups(grantee.ref, platform, stores.cache) == []
    await app.handle(message("/admin", dm=True, sender=grantee))
    assert platform.sent == []
    await app.handle(BotAddedToChat(new_group, grantee))
    assert platform.left == [new_group]
    assert new_group.key not in (await store.snapshot()).admissions
    assert not await access.can_dm(trusted.ref, platform, stores.cache)


@pytest.mark.parametrize("restriction", ["block", "pause"])
async def test_global_restrictions_stop_dm_grantee_before_responder_and_plugins(restriction):
    from chatcore.administration.store import global_scope

    owner, grantee = user("owner"), user(f"grantee-{restriction}")
    stores, platform = memory_stores(), FakePlatform()
    store = MemoryAdministrationStore(stores.repo)
    owners = frozenset({owner.ref.key})
    await store.apply(
        actor=owner.ref,
        owners=owners,
        expected_revision=0,
        change=AdminChange("dm_grant", global_scope("fake"), grantee.ref),
    )
    await store.apply(
        actor=owner.ref,
        owners=owners,
        expected_revision=1,
        change=AdminChange(
            restriction,
            global_scope("fake"),
            grantee.ref,
            3600 if restriction == "pause" else None,
        ),
    )

    class ProbePlugin(BasePlugin):
        name = "probe"

        def __init__(self):
            self.messages = 0

        async def on_message(self, incoming, stores):
            self.messages += 1

    class ProbeResponder:
        def __init__(self):
            self.calls = 0

        async def respond(self, incoming, text, app):
            self.calls += 1

    plugin, responder = ProbePlugin(), ProbeResponder()
    logged = 0
    original_log = stores.repo.log_message

    async def log_message(incoming):
        nonlocal logged
        logged += 1
        await original_log(incoming)

    stores.repo.log_message = log_message
    access = AccessPolicy([], [], owners=owners, store=store)
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[plugin],
        responder=responder,
        bot_name="sbbot",
    )
    # Existing event ordering logs and runs plugin hooks before the restriction check.
    await app.handle(message("hello", dm=True, sender=grantee))
    assert platform.texts
    expected = "can't use" if restriction == "block" else "paused"
    assert expected in platform.texts[-1]
    assert responder.calls == 0
    assert not any(item.action_id for item in platform.sent)


async def test_dm_request_notice_enforces_bot_wide_hourly_cap():
    stores = memory_stores()
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([], []),
        bot_name="sbbot",
        dm_request_notice=True,
    )
    for index in range(31):
        incoming = message("hello", dm=True, sender=user(f"stranger-{index}"))
        await app.handle(incoming)
    assert len(platform.sent) == 30
    # Repeated messages after the cap stay denied and never enter message history.
    for index in range(31):
        chat = ChatRef("fake", f"stranger-{index}")
        assert await stores.repo.recent_messages(chat, limit=1) == []


async def test_owner_dm_grant_and_revoke_round_trip_through_confirmation():
    from chatcore.administration.plugin import AdministrationPlugin

    owner, friend = user("owner"), user("friend")
    stores = memory_stores()
    store = MemoryAdministrationStore(stores.repo)
    access = AccessPolicy([], [], owners=[owner.ref.key], store=store)
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[AdministrationPlugin(access, store, frozenset({owner.ref.key}))],
        bot_name="sbbot",
    )
    await app.handle(message(f"/admin dmgrant {friend.ref.key} 1h", dm=True, sender=owner))
    assert not (await store.snapshot()).dm_grants
    confirmation = platform.sent[-1]
    assert "private DM" in confirmation.text and "daily budget" in confirmation.text
    await app.handle(press(confirmation.action_id, sender=owner, chat=confirmation.chat))
    assert friend.ref.key in (await store.snapshot()).dm_grants
    assert "DM access granted" in platform.texts[-1]

    await app.handle(message(f"/admin dmrevoke {friend.ref.key}", dm=True, sender=owner))
    confirmation = platform.sent[-1]
    await app.handle(press(confirmation.action_id, sender=owner, chat=confirmation.chat))
    assert friend.ref.key not in (await store.snapshot()).dm_grants
    assert f"DM access removed for {friend.ref.key}" in platform.texts[-1]


async def test_dm_grants_are_refused_for_non_owner_roles_and_group_commands():
    owner, store, _stores, platform, _access, app = build_dm_admin_app()
    admin = user("admin")
    delegate = user("delegate")
    app.access.admin_users = frozenset({admin.ref.key})
    store._assignments.add((delegate.ref.key, GROUP.key))
    await app.access.refresh()

    await app.handle(message("/admin dmgrant fake:friend", dm=True, sender=admin))
    await app.handle(message("/admin dmgrant fake:friend", dm=True, sender=delegate))
    assert not (await store.snapshot()).dm_grants
    assert not any(item.action_id for item in platform.sent)
    assert "Owner access" in platform.texts[-1]

    await app.handle(message("/admin dmgrant fake:friend", chat=GROUP, sender=owner))
    assert "private chat" in platform.texts[-1]
    assert not (await store.snapshot()).dm_grants


async def test_dm_confirmation_rejects_other_requester_chat_and_replay():
    owner, store, _stores, platform, _access, app = build_dm_admin_app()
    friend = user("friend")
    await app.handle(message(f"/admin dmgrant {friend.ref.key}", dm=True, sender=owner))
    action = platform.sent[-1]
    await app.handle(press(action.action_id, sender=user("other"), chat=action.chat))
    await app.handle(press(action.action_id, sender=owner, chat=ChatRef("fake", "elsewhere")))
    assert not (await store.snapshot()).dm_grants
    await app.handle(press(action.action_id, sender=owner, chat=action.chat))
    granted = await store.snapshot()
    assert friend.ref.key in granted.dm_grants
    await app.handle(press(action.action_id, sender=owner, chat=action.chat))
    assert (await store.snapshot()).revision == granted.revision


async def test_stale_dm_confirmation_changes_nothing():
    owner, store, _stores, platform, _access, app = build_dm_admin_app()
    friend = user("friend")
    await app.handle(message(f"/admin dmgrant {friend.ref.key}", dm=True, sender=owner))
    action = platform.sent[-1]
    await store.apply(
        actor=owner.ref,
        owners=frozenset({owner.ref.key}),
        expected_revision=0,
        change=AdminChange("disable", GROUP),
    )
    await app.handle(press(action.action_id, sender=owner, chat=action.chat))
    assert friend.ref.key not in (await store.snapshot()).dm_grants
    assert "changed" in platform.texts[-1].lower()


@pytest.mark.parametrize("target", ["tg:-2", "tg:0", "fake:2", "tg:1", "tg:99"])
async def test_dm_command_rejects_invalid_cross_platform_owner_and_self_targets(target):
    from chatcore.administration.plugin import AdministrationPlugin

    owner = User(UserRef("tg", "1"), "Owner")
    store, stores, platform = MemoryAdministrationStore(), memory_stores(), FakePlatform(name="tg")
    owners = frozenset({owner.ref.key, "tg:99"})
    access = AccessPolicy([], [], owners=owners, store=store)
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[AdministrationPlugin(access, store, owners)],
        bot_name="sbbot",
    )

    def owner_dm(text):
        incoming = message(text, dm=True, sender=owner)
        return replace(incoming, chat=ChatRef("tg", owner.ref.id))

    await app.handle(owner_dm("/admin dmgrant tg:2"))
    confirmation = platform.sent[-1]
    assert confirmation.action_id
    await app.handle(press(confirmation.action_id, sender=owner, chat=confirmation.chat))
    assert "tg:2" in (await store.snapshot()).dm_grants

    previous_confirmation_count = sum(item.action_id is not None for item in platform.sent)
    await app.handle(owner_dm(f"/admin dmgrant {target}"))
    assert set((await store.snapshot()).dm_grants) == {"tg:2"}
    assert sum(item.action_id is not None for item in platform.sent) == previous_confirmation_count
    assert "Usage" in platform.texts[-1] or "valid" in platform.texts[-1].lower()


@pytest.mark.parametrize("duration", ["0m", "366d", "broken"])
async def test_dm_command_rejects_invalid_duration(duration):
    owner, store, _stores, platform, _access, app = build_dm_admin_app()
    await app.handle(message(f"/admin dmgrant fake:friend {duration}", dm=True, sender=owner))
    assert not (await store.snapshot()).dm_grants
    assert not any(item.action_id for item in platform.sent)


async def test_dm_command_accepts_365d_and_no_op_regrant_does_not_audit():
    owner, store, _stores, platform, _access, app = build_dm_admin_app()
    friend = user("friend")
    await app.handle(message(f"/admin dmgrant {friend.ref.key} 365d", dm=True, sender=owner))
    action = platform.sent[-1]
    assert action.action_id
    await app.handle(press(action.action_id, sender=owner, chat=action.chat))
    assert friend.ref.key in (await store.snapshot()).dm_grants

    # An unbounded grant makes the same request exactly idempotent.
    await app.handle(message(f"/admin dmgrant {friend.ref.key}", dm=True, sender=owner))
    action = platform.sent[-1]
    await app.handle(press(action.action_id, sender=owner, chat=action.chat))
    revision = (await store.snapshot()).revision
    audit_count = len(await store.audit())
    await app.handle(message(f"/admin dmgrant {friend.ref.key}", dm=True, sender=owner))
    action = platform.sent[-1]
    await app.handle(press(action.action_id, sender=owner, chat=action.chat))
    assert (await store.snapshot()).revision == revision
    assert len(await store.audit()) == audit_count


async def test_dm_regrant_replaces_expiry_and_listing_hides_expired_rows():
    from datetime import UTC, datetime, timedelta

    owner, store, _stores, platform, _access, app = build_dm_admin_app()
    friend = user("friend")
    for duration in ("1h", "2h"):
        await app.handle(
            message(f"/admin dmgrant {friend.ref.key} {duration}", dm=True, sender=owner)
        )
        action = platform.sent[-1]
        await app.handle(press(action.action_id, sender=owner, chat=action.chat))
    snapshot = await store.snapshot()
    assert snapshot.revision == 2 and len(await store.audit()) == 2
    assert snapshot.dm_grants[friend.ref.key].expires_at > (
        datetime.now(UTC) + timedelta(minutes=100)
    )

    store._dm_grants["fake:expired"] = DmGrant(
        owner.ref.key, datetime.now(UTC) - timedelta(days=2), datetime.now(UTC) - timedelta(days=1)
    )
    await app.handle(message("/admin dmgrants", dm=True, sender=owner))
    assert friend.ref.key in platform.texts[-1]
    assert "fake:expired" not in platform.texts[-1]


async def test_non_owner_admin_menu_does_not_show_dm_commands():
    owner, store, _stores, platform, _access, app = build_dm_admin_app()
    delegate = user("delegate")
    store._assignments.add((delegate.ref.key, GROUP.key))
    await app.access.refresh()
    await app.handle(message("/admin", dm=True, sender=delegate))
    assert platform.texts
    assert "dmgrant" not in platform.texts[-1]
    assert "dmgrants" not in platform.texts[-1]


@pytest.mark.asyncio
async def test_revoked_delegate_loses_setup_dm_access_without_group_membership():
    owner, delegate = UserRef("fake", "owner"), UserRef("fake", "delegate")
    group = ChatRef("fake", "group")
    store = MemoryAdministrationStore()
    owners = frozenset({owner.key})
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("grant", group, user=delegate),
    )
    policy = AccessPolicy([group.key], [], owners=owners, store=store)
    await policy.refresh()
    platform = FakePlatform()
    stores = memory_stores()
    assert await policy.can_dm(delegate, platform, stores.cache)
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=1,
        change=AdminChange("revoke", group, user=delegate),
    )
    await policy.refresh()
    assert not await policy.can_dm(delegate, platform, stores.cache)


@pytest.mark.asyncio
async def test_owner_grant_and_scoped_delegate_enable_require_dm_confirmation():
    from chatcore.administration.plugin import AdministrationPlugin

    owner, helper = UserRef("fake", "owner"), UserRef("fake", "helper")
    group = ChatRef("fake", "trip")
    store = MemoryAdministrationStore()
    platform = FakePlatform()
    access = AccessPolicy([group.key], [], owners=[owner.key], store=store)
    await access.refresh()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[AdministrationPlugin(access, store, frozenset({owner.key}))],
        bot_name="sbbot",
    )
    await app.handle(message("/admin grant fake:helper fake:trip", dm=True, sender=user("owner")))
    assert not (await store.snapshot()).assignments
    assert await app.stores.repo.recent_messages(ChatRef("fake", "owner"), limit=10) == []
    grant = platform.sent[-1]
    await app.handle(press(grant.action_id, sender=user("owner"), chat=grant.chat))
    assert (helper.key, group.key) in (await store.snapshot()).assignments
    await app.handle(message("/admin enable fake:trip", dm=True, sender=user("helper")))
    enable = platform.sent[-1]
    assert (await store.snapshot()).overrides.get(group.key) is None
    await app.handle(press(enable.action_id, sender=user("helper"), chat=enable.chat))
    assert (await store.snapshot()).overrides[group.key] is True
    assert not app.access.is_admin(helper)


@pytest.mark.asyncio
async def test_revoked_permission_after_prompt_cannot_be_used_even_after_regrant():
    from chatcore.administration.plugin import AdministrationPlugin

    owner, helper = UserRef("fake", "owner"), UserRef("fake", "helper")
    group = ChatRef("fake", "trip")
    owners = frozenset({owner.key})
    store = MemoryAdministrationStore()
    platform = FakePlatform()
    access = AccessPolicy([group.key], [], owners=owners, store=store)
    await access.refresh()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[AdministrationPlugin(access, store, owners)],
        bot_name="sbbot",
    )
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("grant", group, user=helper),
    )
    await access.refresh()
    await app.handle(message("/admin enable fake:trip", dm=True, sender=user("helper")))
    prompt = platform.sent[-1]
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=1,
        change=AdminChange("revoke", group, user=helper),
    )
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=2,
        change=AdminChange("grant", group, user=helper),
    )
    await app.handle(press(prompt.action_id, sender=user("helper"), chat=prompt.chat))
    assert (await store.snapshot()).overrides == {}
    assert "changed since" in platform.texts[-1]


@pytest.mark.asyncio
async def test_group_admin_invocation_only_directs_owner_to_dm_and_does_not_write():
    from chatcore.administration.plugin import AdministrationPlugin

    owner = UserRef("fake", "owner")
    store = MemoryAdministrationStore()
    platform = FakePlatform()
    access = AccessPolicy([GROUP.key], [], owners=[owner.key], store=store)
    await access.refresh()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[AdministrationPlugin(access, store, frozenset({owner.key}))],
        bot_name="sbbot",
    )
    await app.handle(message("/admin disable fake:group", sender=user("owner")))
    assert (await store.snapshot()).overrides == {}
    assert "private chat" in platform.texts[-1]
    logged = await app.stores.repo.recent_messages(GROUP, limit=10)
    assert all(item.is_bot for item in logged)


@pytest.mark.asyncio
async def test_admin_reads_and_audit_are_limited_to_delegate_assignments():
    from chatcore.administration.plugin import AdministrationPlugin

    owner, first, second = (UserRef("fake", key) for key in ("owner", "first", "second"))
    group_a, group_b = ChatRef("fake", "alpha"), ChatRef("fake", "beta")
    owners = frozenset({owner.key})
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("grant", group_a, user=first),
    )
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=1,
        change=AdminChange("grant", group_b, user=second),
    )
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=2,
        change=AdminChange("enable", group_a),
    )
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=3,
        change=AdminChange("enable", group_b),
    )
    access = AccessPolicy([group_a.key, group_b.key], [], owners=owners, store=store)
    await access.refresh()
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[AdministrationPlugin(access, store, owners)],
        bot_name="sbbot",
    )
    for command in ("/admin groups", "/admin admins", "/admin audit"):
        await app.handle(message(command, dm=True, sender=user("first")))
    replies = "\n".join(platform.texts)
    assert group_a.key in replies
    assert group_b.key not in replies
    assert second.key not in replies


async def test_global_scope_bootstrap_is_ignored_and_groups_does_not_query_it(caplog):
    from chatcore.administration.plugin import AdministrationPlugin
    from chatcore.administration.store import AccessSnapshot

    owner = UserRef("fake", "owner")
    stores = memory_stores()
    queried = []
    original_repo = stores.repo

    class RecordingRepo:
        def __getattr__(self, name):
            method = getattr(original_repo, name)
            if name == "get_chat_settings":

                async def get_chat_settings(chat):
                    queried.append(chat)
                    return await method(chat)

                return get_chat_settings
            return method

    stores.repo = RecordingRepo()
    access = AccessPolicy(["console:*"], [], owners=[owner.key])
    assert not access.is_chat_allowed(ChatRef("console", "*"))
    access._snapshot = AccessSnapshot(0, {"console:*": True}, frozenset())
    assert not access.is_chat_allowed(ChatRef("console", "*"))
    assert any(
        "ignored invalid bootstrap chats count=1" == row.getMessage() for row in caplog.records
    )
    assert all("console:*" not in row.getMessage() for row in caplog.records)
    store = MemoryAdministrationStore()
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[AdministrationPlugin(access, store, frozenset({owner.key}))],
        bot_name="sbbot",
    )

    await app.handle(message("/admin groups", dm=True, sender=user("owner")))

    assert ChatRef("console", "*") not in queried


@pytest.mark.asyncio
async def test_fabricated_action_payload_with_extra_target_is_rejected():
    from chatcore.administration.plugin import AdministrationPlugin
    from chatcore.pending import PendingAction

    owner = user("owner")
    group = ChatRef("fake", "trip")
    owners = frozenset({owner.ref.key})
    store = MemoryAdministrationStore()
    access = AccessPolicy([group.key], [], owners=owners, store=store)
    platform = FakePlatform()
    plugin = AdministrationPlugin(access, store, owners)
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[plugin],
        bot_name="sbbot",
    )
    action = PendingAction.create(
        chat=ChatRef("fake", owner.ref.id),
        requester=owner.ref,
        plugin="administration",
        kind="change",
        payload={
            "operation": "enable",
            "group": group.key,
            "user": None,
            "value": None,
            "expected_revision": 0,
            "other_group": "fake:elsewhere",
        },
        summary="",
    )
    ctx = app.command_context(action.chat, owner, is_dm=True)
    reply = await plugin.commit(action, ctx)
    assert "invalid" in reply
    assert (await store.snapshot()).overrides == {}


@pytest.mark.asyncio
async def test_wrong_requester_cannot_claim_admin_action_and_replay_is_single_use():
    from chatcore.administration.plugin import AdministrationPlugin

    owner, helper = UserRef("fake", "owner"), UserRef("fake", "helper")
    group = ChatRef("fake", "trip")
    owners = frozenset({owner.key})
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("grant", group, user=helper),
    )
    platform = FakePlatform()
    access = AccessPolicy([group.key], [], owners=owners, store=store)
    await access.refresh()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[AdministrationPlugin(access, store, owners)],
        bot_name="sbbot",
    )
    await app.handle(message("/admin enable fake:trip", dm=True, sender=user("helper")))
    action_msg = platform.sent[-1]
    await app.handle(press(action_msg.action_id, sender=user("stranger"), chat=action_msg.chat))
    assert (await store.snapshot()).revision == 1
    await app.handle(press(action_msg.action_id, sender=user("helper"), chat=action_msg.chat))
    assert (await store.snapshot()).revision == 2
    await app.handle(press(action_msg.action_id, sender=user("helper"), chat=action_msg.chat))
    assert (await store.snapshot()).revision == 2


@pytest.mark.asyncio
async def test_admin_reply_code_fallback_confirms_and_delegate_cannot_use_purge():
    from chatcore.administration.plugin import AdministrationPlugin
    from chatcore.plugin import BasePlugin, Command

    owner, helper = UserRef("fake", "owner"), UserRef("fake", "helper")
    group = ChatRef("fake", "trip")
    owners = frozenset({owner.key})
    store = MemoryAdministrationStore()
    await store.apply(
        actor=owner,
        owners=owners,
        expected_revision=0,
        change=AdminChange("grant", group, user=helper),
    )
    platform = FakePlatform(capabilities=replace(FakePlatform().capabilities, buttons=False))

    class Destructive(BasePlugin):
        name = "destructive"

        def commands(self):
            async def purge(ctx, args):
                return "purged"

            return [Command("purgechat", "Delete chat data", purge, admin_only=True)]

    access = AccessPolicy([group.key], [], owners=owners, store=store)
    await access.refresh()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[AdministrationPlugin(access, store, owners), Destructive()],
        bot_name="sbbot",
    )
    await app.handle(message("/admin enable fake:trip", dm=True, sender=user("helper")))
    code = platform.texts[-1].split("YES ", maxsplit=1)[1].split()[0]
    assert (await store.snapshot()).revision == 1
    await app.handle(message(f"YES {code}", dm=True, sender=user("helper")))
    assert (await store.snapshot()).revision == 2
    await app.handle(
        message("/admin grant fake:stranger fake:trip", dm=True, sender=user("helper"))
    )
    assert "Owner access" in platform.texts[-1]
    assert (await store.snapshot()).revision == 2
    await app.handle(message("/purgechat", dm=True, sender=user("helper")))
    assert platform.texts[-1] != "purged"


async def test_owner_trust_and_group_blocks_require_confirmation_and_show_provenance():
    from chatcore.administration.plugin import AdministrationPlugin

    owner, friend = user("owner"), user("friend")
    store, platform, stores = MemoryAdministrationStore(), FakePlatform(), memory_stores()
    access = AccessPolicy([], [], owners=[owner.ref.key], store=store)
    app = BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[AdministrationPlugin(access, store, frozenset({owner.ref.key}))],
        bot_name="sbbot",
    )
    await app.handle(message("/admin trust fake:friend", dm=True, sender=owner))
    assert not (await store.snapshot()).trusted_inviters
    pending = platform.sent[-1]
    assert "invite" in pending.text.lower()
    await app.handle(press(pending.action_id, sender=owner, chat=pending.chat))
    assert friend.ref.key in (await store.snapshot()).trusted_inviters
    await app.handle(message("/admin trusted", dm=True, sender=owner))
    assert friend.ref.key in platform.texts[-1]
    before = len(platform.sent)
    await app.handle(message("/admin trusted", dm=True, sender=friend))
    assert len(platform.sent) == before  # Trust alone does not grant DM admission.
    await app.handle(message("/admin blockgroup fake:group", dm=True, sender=owner))
    pending = platform.sent[-1]
    assert GROUP.key not in (await store.snapshot()).blocked_groups
    await app.handle(press(pending.action_id, sender=owner, chat=pending.chat))
    assert GROUP.key in (await store.snapshot()).blocked_groups
    await app.handle(message("/admin enable fake:group", dm=True, sender=owner))
    assert "blocked" in platform.texts[-1]
    await app.handle(message("/admin unblockgroup fake:group", dm=True, sender=owner))
    pending = platform.sent[-1]
    assert "enable" in pending.text.lower()
    await app.handle(press(pending.action_id, sender=owner, chat=pending.chat))
    assert not access.is_chat_allowed(GROUP)
    await app.handle(message("/admin untrust fake:friend", dm=True, sender=owner))
    pending = platform.sent[-1]
    assert "existing" in pending.text.lower()
    await app.handle(press(pending.action_id, sender=owner, chat=pending.chat))
    assert not (await store.snapshot()).trusted_inviters


@pytest.mark.parametrize(
    "command",
    [
        "trust fake:other",
        "untrust fake:other",
        "blockgroup fake:group",
        "unblockgroup fake:group",
        "trusted",
    ],
)
async def test_delegates_cannot_manage_invitation_permissions(command):
    from chatcore.administration.plugin import AdministrationPlugin

    owner, friend = user("owner"), user("friend")
    store, platform = MemoryAdministrationStore(), FakePlatform()
    owners = frozenset({owner.ref.key})
    await store.apply(
        actor=owner.ref,
        owners=owners,
        expected_revision=0,
        change=AdminChange("grant", GROUP, friend.ref),
    )
    access = AccessPolicy([], [], owners=owners, store=store)
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[AdministrationPlugin(access, store, owners)],
        bot_name="sbbot",
    )
    await app.handle(message("/admin " + command, dm=True, sender=friend))
    assert "Owner access" in platform.texts[-1]
    assert (await store.snapshot()).revision == 1


@pytest.mark.parametrize(
    "command",
    ["trust fake:other", "untrust fake:other", "blockgroup fake:group", "unblockgroup fake:group"],
)
async def test_invitation_changes_enforce_requester_staleness_and_single_use(command):
    from chatcore.administration.plugin import AdministrationPlugin

    owner, other = user("owner"), user("other")
    store, platform = MemoryAdministrationStore(), FakePlatform()
    owners = frozenset({owner.ref.key})
    access = AccessPolicy([GROUP.key], [other.ref.key], owners=owners, store=store)
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[AdministrationPlugin(access, store, owners)],
        bot_name="sbbot",
    )
    await app.handle(message("/admin " + command, dm=True, sender=owner))
    pending = platform.sent[-1]
    assert pending.action_id
    await app.handle(press(pending.action_id, sender=other, chat=pending.chat))
    assert (await store.snapshot()).revision == 0
    await store.apply(
        actor=owner.ref,
        owners=owners,
        expected_revision=0,
        change=AdminChange("disable", ChatRef("fake", "another")),
    )
    await app.handle(press(pending.action_id, sender=owner, chat=pending.chat))
    assert (await store.snapshot()).revision == 1
    assert "changed since" in platform.texts[-1]
    await app.handle(message("/admin " + command, dm=True, sender=owner))
    pending = platform.sent[-1]
    await app.handle(press(pending.action_id, sender=owner, chat=pending.chat))
    assert (await store.snapshot()).revision == 2
    await app.handle(press(pending.action_id, sender=owner, chat=pending.chat))
    assert (await store.snapshot()).revision == 2


@pytest.mark.parametrize(
    "command",
    [
        "trust tg:-1",
        "trust tg:*",
        "trust other:2",
        "blockgroup tg:2",
        "blockgroup tg:*",
        "unblockgroup other:-1",
    ],
)
async def test_invitation_commands_reject_malformed_and_cross_platform_targets(command):
    from chatcore.administration.plugin import AdministrationPlugin
    from chatcore.events import User, UserRef

    owner = User(UserRef("tg", "1"), display_name="Owner")
    store, platform = MemoryAdministrationStore(), FakePlatform(name="tg")
    owners = frozenset({owner.ref.key})
    access = AccessPolicy([], [], owners=owners, store=store)
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[AdministrationPlugin(access, store, owners)],
        bot_name="sbbot",
    )
    dm = replace(message("/admin " + command, dm=True, sender=owner), chat=ChatRef("tg", "1"))
    await app.handle(dm)
    assert (await store.snapshot()).revision == 0
    assert platform.sent[-1].action_id is None
    assert "Usage" in platform.texts[-1]
