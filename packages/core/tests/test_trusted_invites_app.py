"""Join-event authorization must precede privacy hooks, logging and model calls."""

from dataclasses import replace

import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.store import AdminChange, MemoryAdministrationStore, global_scope
from chatcore.app import BotApp
from chatcore.events import BotAddedToChat, ChatRef, User, UserRef
from chatcore.plugin import BasePlugin
from chatcore.privacy import PrivacyPlugin
from chatcore.testing import FakePlatform, memory_stores, message

OWNER, FRIEND, STRANGER = (User(UserRef("tg", str(i)), display_name=str(i)) for i in (1, 2, 3))
GROUP = ChatRef("tg", "-1001")
OWNERS = frozenset({OWNER.ref.key})


class JoinRecorder(BasePlugin):
    name = "join_recorder"

    def __init__(self):
        self.joins = []

    async def on_bot_added(self, chat, platform, stores):
        self.joins.append(chat)


async def setup(*, trusted=True, bootstrap=(), store=None):
    store = store or MemoryAdministrationStore()
    if trusted:
        await store.apply(
            actor=OWNER.ref,
            owners=OWNERS,
            expected_revision=0,
            change=AdminChange("trust", global_scope("tg"), FRIEND.ref),
        )
    platform, stores, plugin = FakePlatform(name="tg"), memory_stores(), JoinRecorder()
    access = AccessPolicy(bootstrap, [], owners=OWNERS, store=store)
    app = BotApp(platform, stores=stores, access=access, plugins=[plugin], bot_name="sbbot")
    return app, store, plugin


async def test_trusted_join_admits_before_hooks_and_content_and_replays_do_not_repeat_hooks():
    app, store, plugin = await setup()
    event = BotAddedToChat(GROUP, FRIEND, event_key="99:10")
    await app.handle(event)
    assert plugin.joins == [GROUP]
    assert app.access.is_chat_allowed(GROUP)
    assert not app.platform.left
    await app.handle(event)
    assert plugin.joins == [GROUP]
    await app.handle(replace(event, event_key="99:11"))  # Genuine rejoin.
    assert plugin.joins == [GROUP, GROUP]
    assert len(await store.audit()) == 2
    await app.handle(message("@sbbot hello", chat=GROUP, sender=FRIEND, mention=True))
    assert app.platform.texts == ["echo: hello"]
    assert len(await app.stores.repo.recent_messages(GROUP, limit=10)) == 2


@pytest.mark.parametrize("inviter", [STRANGER, None])
async def test_denied_join_has_no_hook_or_content_side_effect(inviter):
    app, store, plugin = await setup()
    await app.handle(BotAddedToChat(GROUP, inviter))
    await app.handle(message("@sbbot private words", chat=GROUP, sender=FRIEND, mention=True))
    assert app.platform.left == [GROUP, GROUP]
    assert not app.platform.texts and not plugin.joins
    assert await app.stores.repo.recent_messages(GROUP, limit=10) == []
    assert not (await store.snapshot()).admissions


async def test_early_message_is_denied_then_authorized_reinvite_recovers():
    app, _, plugin = await setup()
    await app.handle(message("early", chat=GROUP, sender=FRIEND))
    assert app.platform.left == [GROUP]
    assert await app.stores.repo.recent_messages(GROUP, limit=10) == []
    await app.handle(BotAddedToChat(GROUP, FRIEND))
    assert plugin.joins == [GROUP]


@pytest.mark.parametrize("role", ["admin", "grant", "moderator_grant"])
async def test_other_roles_do_not_authorize_unknown_group_invites(role):
    app, store, plugin = await setup(trusted=False)
    if role == "admin":
        app.access.admin_users = frozenset({FRIEND.ref.key})
    else:
        await store.apply(
            actor=OWNER.ref,
            owners=OWNERS,
            expected_revision=0,
            change=AdminChange(role, GROUP, FRIEND.ref),
        )
    await app.handle(BotAddedToChat(GROUP, FRIEND))
    assert app.platform.left == [GROUP] and not plugin.joins


async def test_admission_storage_failure_cannot_run_hooks_or_record_content():
    class BrokenAdmissionStore(MemoryAdministrationStore):
        async def admit_group(self, **kwargs):
            raise OSError("private database failure text")

    app, _, plugin = await setup(store=BrokenAdmissionStore())
    await app.handle(BotAddedToChat(GROUP, FRIEND))
    assert not plugin.joins and not app.access.is_chat_allowed(GROUP)
    assert not app.platform.texts
    assert await app.stores.repo.recent_messages(GROUP, limit=10) == []


async def test_newly_admitted_group_gets_existing_privacy_notice():
    app, _, _ = await setup()
    privacy = PrivacyPlugin([], provider_label="test provider")
    app.plugins.append(privacy)
    await app.handle(BotAddedToChat(GROUP, FRIEND))
    assert len(app.platform.texts) == 1
    assert "test provider" in app.platform.texts[0]


async def test_owner_group_listing_shows_admission_provenance_after_untrust():
    from chatcore.administration.plugin import AdministrationPlugin

    app, store, recorder = await setup()
    app = BotApp(
        app.platform,
        stores=app.stores,
        access=app.access,
        plugins=[recorder, AdministrationPlugin(app.access, store, OWNERS)],
        bot_name="sbbot",
    )
    await app.handle(BotAddedToChat(GROUP, FRIEND, event_key="99:20"))
    await store.apply(
        actor=OWNER.ref,
        owners=OWNERS,
        expected_revision=2,
        change=AdminChange("untrust", global_scope("tg"), FRIEND.ref),
    )
    await app.handle(
        replace(message("/admin groups", dm=True, sender=OWNER), chat=ChatRef("tg", "1"))
    )
    assert f"{GROUP.key}: enabled" in app.platform.texts[-1]
    assert f"admitted by {FRIEND.ref.key}" in app.platform.texts[-1]
    assert app.access.assigned_groups(FRIEND.ref) == frozenset()


async def test_failed_leave_does_not_authorize_or_log_denied_content(caplog):
    from chatcore.platform import PlatformError

    app, _, plugin = await setup()

    async def failed_leave(chat):
        raise PlatformError("private failure details")

    app.platform.leave = failed_leave
    await app.handle(BotAddedToChat(GROUP, STRANGER))
    await app.handle(
        message("@sbbot denied secret content", chat=GROUP, sender=STRANGER, mention=True)
    )
    assert not plugin.joins and not app.platform.texts
    assert await app.stores.repo.recent_messages(GROUP, limit=10) == []
    assert "denied secret content" not in caplog.text
    assert "private failure details" not in caplog.text
