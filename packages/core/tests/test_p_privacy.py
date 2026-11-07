import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.display_time_plugin import DisplayTimePlugin
from chatcore.display_time_store import display_service
from chatcore.events import BotAddedToChat, ChatRef
from chatcore.pending import PendingAction
from chatcore.plugin import BasePlugin, CommandContext
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
    assert "incomplete" in platform.texts[-1].lower()
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


async def test_reader_plugin_failure_keeps_core_messages_for_a_retry():
    class Reader(BasePlugin):
        name = "reader"
        purge_reads_messages = True

        def __init__(self):
            self.failed = False

        async def purge_user(self, chat, user_ref, stores):
            if not self.failed:
                self.failed = True
                raise RuntimeError("private failure")

    alice = user("alice")
    reader = Reader()
    app, platform, privacy = make_app((reader,))
    await app.stores.repo.log_message(message("keep until retry", sender=alice))
    await app.handle(message("/forgetme", sender=alice))
    await app.handle(press(platform.sent[-1].action_id, sender=alice))

    rows = await app.stores.repo.recent_messages(GROUP, limit=10)
    assert any(row.text == "keep until retry" for row in rows)
    assert "incomplete" in platform.texts[-1]
    operation = (await app.stores.repo.list_erasures(GROUP))[0]

    done, _count = await privacy._process_erasure(operation, app.stores, force=True)

    assert done
    remaining = await app.stores.repo.recent_messages(GROUP, limit=10)
    assert not any(row.text == "keep until retry" for row in remaining)
    assert await app.stores.repo.list_erasures(GROUP) == []


async def test_failed_erasure_backs_off_when_another_plugin_returns_a_count():
    class Counting(BasePlugin):
        name = "counting"

        async def purge_user(self, chat, user_ref, stores):
            return 3

    class Failing(BasePlugin):
        name = "failing"

        async def purge_user(self, chat, user_ref, stores):
            raise RuntimeError("private failure")

    alice = user("alice")
    app, _platform, privacy = make_app((Failing(), Counting()))
    operation = await app.stores.repo.create_erasure("operation-a", GROUP, "user", alice.ref.key)

    done, count = await privacy._process_erasure(operation, app.stores, force=True)

    assert not done
    assert count == 3
    row = (await app.stores.repo.list_erasures(GROUP))[0]
    assert row["attempts"] == 1
    assert row["last_error_code"] == "RuntimeError"
    assert row["next_attempt_at"] > privacy.clock()


async def test_erasure_for_same_chat_is_queued_then_promoted():
    alice = user("alice")
    app, _platform, privacy = make_app()
    first = await app.stores.repo.create_erasure("operation-a", GROUP, "user", alice.ref.key)
    second = await app.stores.repo.create_erasure("operation-b", GROUP, "user", alice.ref.key)
    assert second["status"] == "queued"

    done, _count = await privacy._process_erasure(first, app.stores)

    assert done
    outstanding = await app.stores.repo.list_erasures(GROUP)
    assert outstanding == []


async def test_new_erasure_queues_behind_queued_operation_and_promotes_in_order():
    repo = MemoryCoreRepo()
    first = await repo.create_erasure("op-a", GROUP, "user", "fake:a")
    second = await repo.create_erasure("op-b", GROUP, "user", "fake:b")
    assert (first["status"], second["status"]) == ("active", "queued")
    # A finished and was deleted, but B has not been promoted yet (or promotion crashed).
    await repo.delete_erasure("op-a")

    third = await repo.create_erasure("op-c", GROUP, "user", "fake:c")

    assert third["status"] == "queued"
    assert (await repo.promote_erasure(GROUP))["operation_id"] == "op-b"
    assert await repo.promote_erasure(GROUP) is None
    await repo.delete_erasure("op-b")
    assert (await repo.promote_erasure(GROUP))["operation_id"] == "op-c"


@pytest.mark.parametrize("kind", ["forgetme", "purgechat"])
async def test_new_erasure_behind_unpromoted_queue_runs_older_first_and_completes(kind):
    calls = []

    class OrderPlugin(BasePlugin):
        name = "order"

        async def purge_user(self, chat, user_ref, stores):
            calls.append(("user", user_ref.key))

        async def purge_chat(self, chat, stores):
            calls.append(("chat", chat.key))

    alice = user("alice")
    privacy = PrivacyPlugin([OrderPlugin()], provider_label="AI")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache(prefix=f"privacy-order-{kind}:"))
    await stores.repo.create_erasure("op-a", GROUP, "user", "fake:first")
    await stores.repo.create_erasure("op-b", GROUP, "user", "fake:queued")
    await stores.repo.delete_erasure("op-a")
    action = PendingAction.create(
        chat=GROUP,
        requester=alice.ref,
        plugin="privacy",
        kind=kind,
        payload={"chats": [GROUP.key]} if kind == "forgetme" else {"chat": GROUP.key},
        summary="delete",
    )
    ctx = CommandContext(GROUP, alice, False, kind == "purgechat", FakePlatform(), stores)

    result = (
        await privacy._forgetme(action, ctx)
        if kind == "forgetme"
        else await privacy._purgechat(action, ctx)
    )

    assert "incomplete" not in result.lower()
    expected_new = ("user", alice.ref.key) if kind == "forgetme" else ("chat", GROUP.key)
    assert calls == [("user", "fake:queued"), expected_new]
    assert await stores.repo.list_erasures(GROUP) == []


async def test_recovery_promotes_queued_operation_after_promotion_crash():
    alice = user("alice")
    app, _platform, privacy = make_app()
    first = await app.stores.repo.create_erasure("operation-first", GROUP, "user", alice.ref.key)
    await app.stores.repo.create_erasure("operation-queued", GROUP, "user", alice.ref.key)
    promote = app.stores.repo.promote_erasure
    calls = 0

    async def fail_once(chat):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("promotion unavailable")
        return await promote(chat)

    app.stores.repo.promote_erasure = fail_once

    done, _ = await privacy._process_erasure(first, app.stores)

    assert done is True
    queued = await app.stores.repo.list_erasures(GROUP)
    assert len(queued) == 1 and queued[0]["status"] == "queued"
    await privacy.recover_erasures(app.stores)
    assert await app.stores.repo.list_erasures(GROUP) == []


async def test_promoted_erasure_records_all_begin_steps_and_does_not_change_first_result():
    calls = []

    class HookPlugin(BasePlugin):
        def __init__(self, name, fail_for):
            self.name = name
            self.fail_for = fail_for

        async def begin_erasure(self, chat, operation_id, scope, target, stores):
            calls.append(("begin", self.name, operation_id))

        async def purge_user(self, chat, user_ref, stores):
            if user_ref.id == self.fail_for:
                raise RuntimeError("queued purge failure")

    first_plugin = HookPlugin("first_plugin", "never")
    second_plugin = HookPlugin("second_plugin", "queued-user")
    app, _platform, privacy = make_app((first_plugin, second_plugin))
    first = await app.stores.repo.create_erasure("operation-a", GROUP, "user", "fake:first-user")
    await app.stores.repo.create_erasure("operation-b", GROUP, "user", "fake:queued-user")

    done, _ = await privacy._process_erasure(first, app.stores)

    assert done is True
    outstanding = await app.stores.repo.list_erasures(GROUP)
    assert len(outstanding) == 1
    assert set(outstanding[0]["completed_steps"]) >= {
        "begin:first_plugin",
        "begin:second_plugin",
    }
    assert ("begin", "first_plugin", "operation-b") in calls
    assert ("begin", "second_plugin", "operation-b") in calls


async def test_startup_recovery_resumes_journal_steps_and_keeps_other_chats_moving():
    class FlakyReader(BasePlugin):
        name = "reader"
        purge_reads_messages = True

        def __init__(self, blocked_chat):
            self.blocked_chat = blocked_chat
            self.calls = []

        async def purge_user(self, chat, user_ref, stores):
            self.calls.append(chat.key)
            if chat.key == self.blocked_chat:
                raise RuntimeError("private failure")

    async def blocked_sleep(_seconds):
        await asyncio.Future()

    repo = MemoryCoreRepo()
    stores = Stores(repo=repo, cache=MemoryCache(prefix="privacy-recovery:"))
    first_chat = ChatRef("fake", "recovery-blocked")
    second_chat = ChatRef("fake", "recovery-progress")
    alice = user("alice")
    first = await repo.create_erasure("recovery-a", first_chat, "user", alice.ref.key)
    second = await repo.create_erasure("recovery-b", second_chat, "user", alice.ref.key)
    await repo.update_erasure(first["operation_id"], completed_steps=["begin:reader"])
    await repo.update_erasure(second["operation_id"], completed_steps=["begin:reader"])
    reader = FlakyReader(first_chat.key)
    privacy = PrivacyPlugin([reader], provider_label="AI", sleep=blocked_sleep)

    await privacy.on_startup(stores)
    try:
        outstanding = await repo.list_erasures()
        assert [row["operation_id"] for row in outstanding] == ["recovery-a"]
        assert reader.calls == [first_chat.key, second_chat.key]
    finally:
        tasks = [privacy._erasure_task, privacy._retention_task]
        for task in tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


async def test_startup_survives_operation_exception_and_processes_other_chats():
    async def blocked_sleep(_seconds):
        await asyncio.Future()

    repo = MemoryCoreRepo()
    stores = Stores(repo=repo, cache=MemoryCache(prefix="privacy-startup-errors:"))
    first_chat = ChatRef("fake", "startup-error")
    second_chat = ChatRef("fake", "startup-healthy")
    await repo.create_erasure("startup-a", first_chat, "chat", None)
    await repo.create_erasure("startup-b", second_chat, "chat", None)
    privacy = PrivacyPlugin([], provider_label="AI", sleep=blocked_sleep)
    seen = []

    async def fail_one(operation, _stores, *, force=False):
        seen.append(operation["operation_id"])
        if operation["operation_id"] == "startup-a":
            raise TimeoutError("lock unavailable")
        await repo.delete_erasure(operation["operation_id"])
        return True, 0

    privacy._process_erasure = fail_one

    await privacy.on_startup(stores)
    try:
        assert seen == ["startup-a", "startup-b"]
        assert [row["operation_id"] for row in await repo.list_erasures()] == ["startup-a"]
        assert privacy._erasure_task is not None
    finally:
        for task in (privacy._erasure_task, privacy._retention_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (privacy._erasure_task, privacy._retention_task)
                if task is not None
            ),
            return_exceptions=True,
        )


async def test_first_forgetme_attempt_ignores_database_app_clock_skew():
    alice = user("alice")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache(prefix="privacy-clock-skew:"))
    privacy = PrivacyPlugin([], provider_label="AI", clock=lambda: datetime(2000, 1, 1, tzinfo=UTC))
    action = PendingAction.create(
        chat=GROUP,
        requester=alice.ref,
        plugin="privacy",
        kind="forgetme",
        payload={"chats": [GROUP.key]},
        summary="delete",
    )
    ctx = CommandContext(GROUP, alice, False, False, FakePlatform(), stores)

    result = await privacy._forgetme(action, ctx)

    assert "incomplete" not in result
    assert await stores.repo.list_erasures(GROUP) == []
    purge_action = PendingAction.create(
        chat=GROUP,
        requester=alice.ref,
        plugin="privacy",
        kind="purgechat",
        payload={"chat": GROUP.key},
        summary="delete",
    )
    purge_result = await privacy._purgechat(purge_action, ctx)
    assert "incomplete" not in purge_result
    assert await stores.repo.list_erasures(GROUP) == []


async def test_forgetme_reports_invalid_target_and_continues_with_valid_target():
    alice = user("alice")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache(prefix="privacy-invalid-target:"))
    privacy = PrivacyPlugin([], provider_label="AI")
    action = PendingAction.create(
        chat=GROUP,
        requester=alice.ref,
        plugin="privacy",
        kind="forgetme",
        payload={"chats": ["not-a-chat", GROUP.key]},
        summary="delete",
    )
    ctx = CommandContext(GROUP, alice, False, False, FakePlatform(), stores)

    result = await privacy._forgetme(action, ctx)

    assert "invalid chat target" in result.lower()
    assert await stores.repo.list_erasures(GROUP) == []


async def test_forgetme_reports_start_failures_and_continues_other_targets():
    class FailingRepo(MemoryCoreRepo):
        async def create_erasure(self, operation_id, chat, scope, target_user_key):
            if chat.key == "fake:create-fails":
                raise RuntimeError("journal unavailable")
            return await super().create_erasure(operation_id, chat, scope, target_user_key)

    class FailingLockCache(MemoryCache):
        @asynccontextmanager
        async def lock(self, key, *, ttl_s):
            if key == "erasure:fake:process-fails":
                raise TimeoutError("lock unavailable")
            async with super().lock(key, ttl_s=ttl_s):
                yield

    alice = user("alice")
    stores = Stores(repo=FailingRepo(), cache=FailingLockCache(prefix="privacy:"))
    privacy = PrivacyPlugin([], provider_label="AI")
    action = PendingAction.create(
        chat=GROUP,
        requester=alice.ref,
        plugin="privacy",
        kind="forgetme",
        payload={"chats": ["fake:create-fails", "fake:process-fails", "fake:healthy"]},
        summary="delete",
    )
    ctx = CommandContext(GROUP, alice, False, False, FakePlatform(), stores)

    result = await privacy._forgetme(action, ctx)

    assert "could not start" in result.lower()
    assert "incomplete" in result.lower()
    outstanding = await stores.repo.list_erasures()
    assert [row["chat_key"] for row in outstanding] == ["fake:process-fails"]


async def test_purgechat_reports_journal_start_failure_without_claiming_retry():
    class FailingRepo(MemoryCoreRepo):
        async def create_erasure(self, operation_id, chat, scope, target_user_key):
            raise RuntimeError("journal unavailable")

    admin = user("admin")
    stores = Stores(repo=FailingRepo(), cache=MemoryCache(prefix="privacy-purge-start:"))
    privacy = PrivacyPlugin([], provider_label="AI")
    action = PendingAction.create(
        chat=GROUP,
        requester=admin.ref,
        plugin="privacy",
        kind="purgechat",
        payload={"chat": GROUP.key},
        summary="delete",
    )
    ctx = CommandContext(GROUP, admin, False, True, FakePlatform(), stores)

    result = await privacy._purgechat(action, ctx)

    assert "could not start" in result.lower()
    assert "retry automatically" not in result.lower()


async def test_erasure_release_requires_memory_step_and_retries_remaining_work():
    class Writer(BasePlugin):
        name = "memory"
        erasure_blocks_writes = True

        def __init__(self):
            self.released = []

        async def release_erasure(self, chat, operation_id, stores):
            self.released.append(operation_id)

    admin = user("admin")
    writer = Writer()
    app, _platform, privacy = make_app((writer,), admin=(admin,))
    operation = await app.stores.repo.create_erasure(
        "operation-release", GROUP, "user", "fake:alice"
    )
    pending = PendingAction.create(
        chat=GROUP,
        requester=admin.ref,
        plugin=privacy.name,
        kind="erasure_release",
        payload={"operation_id": "operation-release"},
        summary="release",
    )
    command = CommandContext(GROUP, admin, False, True, FakePlatform(), app.stores)

    blocked = await privacy._release_erasure(pending, command)
    assert "cannot be released" in blocked
    assert writer.released == []

    await app.stores.repo.update_erasure(
        operation["operation_id"], completed_steps=["plugin:memory"]
    )
    completed = await privacy._release_erasure(pending, command)

    assert writer.released == ["operation-release"]
    assert completed == "Remaining erasure steps completed."


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


async def test_real_conversation_purge_failure_retries_and_finds_message_targets():
    alice = user("alice")
    repo = MemoryCoreRepo()
    transcript = ConversationStore()
    conversation = ConversationPlugin(store=transcript)
    now = [datetime.now(UTC)]
    privacy = PrivacyPlugin([conversation], provider_label="AI", clock=lambda: now[0])
    stores = Stores(repo=repo, cache=MemoryCache(prefix="conversation-retry:"))
    incoming = message("transcript target", sender=alice)
    await repo.log_message(incoming)
    await transcript.add(
        incoming.chat,
        incoming.thread_id,
        "user",
        {"speaker": {"user_key": alice.ref.key}, "text": incoming.text},
        incoming.message_id,
    )
    original = transcript.purge_user
    failures = 0
    found_targets = []

    async def fail_once(chat, user_ref, message_ids):
        nonlocal failures
        found_targets.extend(message_ids)
        failures += 1
        if failures == 1:
            raise RuntimeError("conversation store unavailable")
        await original(chat, user_ref, message_ids)

    transcript.purge_user = fail_once
    action = PendingAction.create(
        chat=incoming.chat,
        requester=alice.ref,
        plugin="privacy",
        kind="forgetme",
        payload={"chats": [incoming.chat.key]},
        summary="delete",
    )
    ctx = CommandContext(incoming.chat, alice, False, False, FakePlatform(), stores)

    result = await privacy._forgetme(action, ctx)

    assert "incomplete" in result.lower()
    assert [row.message_id for row in await repo.recent_messages(incoming.chat, limit=10)] == [
        incoming.message_id
    ]
    now[0] += timedelta(minutes=10)
    await privacy.recover_erasures(stores)

    assert failures == 2
    assert found_targets == [incoming.message_id, incoming.message_id]
    assert transcript._turns == []
    assert await repo.recent_messages(incoming.chat, limit=10) == []
