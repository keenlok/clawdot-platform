from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.display_time_plugin import DisplayTimePlugin
from chatcore.display_time_store import display_service
from chatcore.events import BotAddedToChat, ChatRef
from chatcore.pending import PendingAction
from chatcore.plugin import BasePlugin
from chatcore.privacy import PrivacyPlugin
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import GROUP, FakePlatform, message, press, user


class OrderedRepo(MemoryCoreRepo):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def purge_user(self, chat, user_ref):
        self.calls.append(("core", chat.key, user_ref.key))
        return await super().purge_user(chat, user_ref)


class Recorder(BasePlugin):
    name = "recorder"

    def __init__(self, calls, *, fail=False):
        self.calls = calls
        self.fail = fail

    async def purge_user(self, chat, user_ref, stores):
        self.calls.append(("plugin", chat.key, user_ref.key))
        if self.fail:
            raise ValueError("private-text-must-not-log")


def make_app(plugins=(), *, repo=None, admin=()):
    platform = FakePlatform()
    stores = Stores(repo=repo or MemoryCoreRepo(), cache=MemoryCache(prefix="privacy:"))
    plugin = PrivacyPlugin(
        plugins, provider_label="Example AI", images_notice="Photos go to the vision provider."
    )
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], [u.ref.key for u in admin]),
        plugins=[*plugins, plugin],
        bot_name="bot",
    )
    return app, platform, plugin


async def test_privacy_disclosure_has_provider_and_deletion_commands_wait_for_confirmation():
    admin = user("admin")
    app, platform, plugin = make_app(admin=(admin,))
    await app.handle(message("/privacy"))
    assert "Example AI" in platform.texts[-1]
    assert "DMs are separate" in platform.texts[-1]
    assert "personal display time zone" in platform.texts[-1]
    assert "Images: Photos go to the vision provider." in platform.texts[-1]

    alice = user("alice")
    bob = user("bob")
    await app.stores.repo.log_message(message("alice private", sender=alice))
    other_chat = ChatRef("fake", "other")
    await app.stores.repo.log_message(message("bob stays", sender=bob))
    await app.stores.repo.log_message(message("alice elsewhere", sender=alice, chat=other_chat))
    await app.handle(message("/forgetme", sender=alice))
    action_id = platform.sent[-1].action_id
    assert action_id
    assert "alice private" in [
        row.text for row in await app.stores.repo.recent_messages(GROUP, limit=10)
    ]
    await app.handle(press(action_id, sender=user("intruder")))
    assert "alice private" in [
        row.text for row in await app.stores.repo.recent_messages(GROUP, limit=10)
    ]
    await app.handle(press(action_id, sender=alice))
    remaining = await app.stores.repo.recent_messages(GROUP, limit=10)
    assert not [row for row in remaining if row.user == alice.ref]
    assert [row.text for row in remaining if row.user == bob.ref] == ["bob stays"]
    assert [row.text for row in await app.stores.repo.recent_messages(other_chat, limit=10)] == [
        "alice elsewhere"
    ]
    assert "Expense records stay" in platform.texts[-1]


async def test_forgetme_deletes_personal_zone_and_keeps_shared_group_zone():
    alice = user("alice")
    dm = ChatRef(alice.ref.platform, alice.ref.id)
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache(prefix="privacy-zone:"))
    platform = FakePlatform()
    platform.members[GROUP.key] = {alice.ref.key}
    display = DisplayTimePlugin()
    privacy = PrivacyPlugin(
        [display],
        provider_label="AI",
        member_groups=lambda _user: _groups(GROUP),
    )
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[display, privacy],
        bot_name="bot",
    )
    service = display_service(stores)
    await service.set_zone(dm, alice.ref, is_dm=True, zone="Europe/London")
    await service.set_zone(GROUP, alice.ref, is_dm=False, zone="Asia/Singapore")

    await app.handle(message("/forgetme", sender=alice, dm=True))
    action_id = platform.sent[-1].action_id
    assert "personal display time-zone preferences" in platform.sent[-1].text
    await app.handle(press(action_id, sender=alice, chat=dm))

    assert await service.resolve(dm, alice.ref, is_dm=True) == ("UTC", "fallback")
    assert await service.resolve(GROUP, alice.ref, is_dm=False) == ("Asia/Singapore", "group")


async def _groups(chat):
    return [chat]


async def test_forgetme_calls_plugins_before_core_and_reports_partial_failure(caplog):
    repo = OrderedRepo()
    calls = repo.calls
    broken = Recorder(calls, fail=True)
    healthy = Recorder(calls)
    app, platform, _ = make_app((broken, healthy), repo=repo)
    alice = user("alice")
    await repo.log_message(message("do-not-leak-this", sender=alice))
    await app.handle(message("/forgetme", sender=alice))
    action_id = platform.sent[-1].action_id
    assert action_id
    await app.handle(press(action_id, sender=alice))
    assert calls == [
        ("plugin", GROUP.key, alice.ref.key),
        ("plugin", GROUP.key, alice.ref.key),
        ("core", GROUP.key, alice.ref.key),
    ]
    assert "partial" in platform.texts[-1].lower() or "could not" in platform.texts[-1].lower()
    assert "do-not-leak-this" not in caplog.text
    assert "ValueError" in caplog.text


async def test_forgetme_dm_scope_is_dm_and_member_groups_only():
    alice = user("alice")
    group_b, foreign = ChatRef("fake", "b"), ChatRef("fake", "foreign")

    async def member_groups(_user):
        return [group_b]

    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache(prefix="privacy:"))
    plugin = PrivacyPlugin([], provider_label="AI", member_groups=member_groups)
    platform = FakePlatform()
    platform.members[GROUP.key] = {alice.ref.key}
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key, group_b.key], []),
        plugins=[plugin],
        bot_name="bot",
    )
    await app.handle(message("/forgetme", sender=alice, dm=True))
    action = await stores.repo.get_pending(ChatRef("fake", "alice"), platform.sent[-1].action_id)
    assert action and action.payload["chats"] == ["fake:alice", group_b.key]
    assert foreign.key not in action.payload["chats"]


async def test_purgechat_requires_admin_and_confirmed_action_names_target():
    admin, alice = user("admin"), user("alice")
    app, platform, _ = make_app(admin=(admin,))
    await app.handle(message("/purgechat fake:target", sender=alice))
    assert platform.sent == []
    await app.handle(message("/purgechat fake:target", sender=admin))
    action_id = platform.sent[-1].action_id
    assert action_id
    assert "everything" in platform.texts[-1]
    assert "ledger" in platform.texts[-1]

    target = ChatRef("fake", "target")
    await app.stores.repo.log_message(message("target data", chat=target))
    await app.handle(press(action_id, sender=admin))
    assert await app.stores.repo.recent_messages(target, limit=10) == []


async def test_purgechat_rejects_global_and_malformed_targets_before_request_and_commit():
    admin = user("admin")
    repo = MemoryCoreRepo()
    repo.purge_chat = AsyncMock(wraps=repo.purge_chat)
    app, platform, plugin = make_app(admin=(admin,), repo=repo)
    for target in ("tg:*", "tg:", "tg:group:extra"):
        await app.handle(message(f"/purgechat {target}", sender=admin))
        assert platform.sent[-1].action_id is None
        assert "Use /purgechat" in platform.texts[-1]

    action = PendingAction.create(
        chat=ChatRef("fake", admin.ref.id),
        requester=admin.ref,
        plugin=plugin.name,
        kind="purgechat",
        payload={"chat": "tg:*"},
        summary="tampered target",
    )
    await app.confirm.request(action, admin)
    await app.handle(press(action.id, sender=admin, chat=action.chat))
    repo.purge_chat.assert_not_awaited()


async def test_join_notice_pins_once_and_non_allowlisted_chat_is_left():
    app, platform, _ = make_app()
    other = ChatRef("fake", "stranger")
    await app.handle(BotAddedToChat(GROUP))
    await app.handle(BotAddedToChat(GROUP))
    await app.handle(BotAddedToChat(other))
    assert len(platform.texts) == 1 and "Example AI" in platform.texts[0]
    assert len(platform.pinned) == 1
    assert platform.left == [other]


async def test_join_notice_remains_when_pin_fails(caplog):
    app, platform, _ = make_app()

    async def fail_pin(_message):
        raise RuntimeError("private platform response")

    platform.pin = fail_pin
    await app.handle(BotAddedToChat(GROUP))
    assert len(platform.texts) == 1
    assert "RuntimeError" in caplog.text
    assert "private platform response" not in caplog.text


async def test_retention_uses_app_clock_and_daily_cache_key():
    class Retainer(BasePlugin):
        name = "retainer"

        def __init__(self):
            self.calls = []

        async def purge_older_than(self, cutoff, stores):
            self.calls.append(cutoff)

    retainer = Retainer()
    app, _platform, privacy = make_app((retainer,))
    now = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
    alice = user("alice")
    old_message = replace(
        message("old retention row", sender=alice), sent_at=datetime(2024, 1, 1, tzinfo=UTC)
    )
    recent_message = replace(message("recent retention row", sender=alice), sent_at=now)
    await app.stores.repo.log_message(old_message)
    await app.stores.repo.log_message(recent_message)
    first = await privacy.run_retention(app.stores, now=now)
    second = await privacy.run_retention(app.stores, now=now)
    assert first["core"] == 1
    assert second == {}
    assert retainer.calls == [datetime(2025, 10, 30, 20, 0, tzinfo=UTC)]
    rows = await app.stores.repo.recent_messages(GROUP, limit=10)
    assert [row.text for row in rows] == ["recent retention row"]
