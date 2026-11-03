import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.plugin import AdministrationPlugin, _parse_tokens
from chatcore.administration.store import (
    AdminChange,
    MemoryAdministrationStore,
    global_scope,
    validate_change,
)
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.events import ChatRef, User, UserRef
from chatcore.limits import BudgetOverride, Limits, LimitsSettings
from chatcore.pending import PendingAction
from chatcore.plugin import CommandContext
from chatcore.store.memory import MemoryCache
from chatcore.testing import FakePlatform, memory_stores, message, press, user
from chatcore.threads import ThreadSettings


class Usage:
    def __init__(self, tokens=0, *, fail_load=False, fail_save=False):
        self.tokens = tokens
        self.fail_load = fail_load
        self.fail_save = fail_save

    async def load(self, day):
        if self.fail_load:
            raise RuntimeError("fake failure")
        return self.tokens

    async def save(self, day, tokens):
        if self.fail_save:
            raise RuntimeError("fake failure")
        self.tokens = max(self.tokens, tokens)


class GatedUsage(Usage):
    def __init__(self, tokens):
        super().__init__(tokens)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def load(self, day):
        value = self.tokens
        self.started.set()
        await self.release.wait()
        return value


class HangingUsage:
    async def load(self, day):
        await asyncio.Event().wait()

    async def save(self, day, tokens):
        await asyncio.Event().wait()


class LockCheckingUsage(Usage):
    def __init__(self, cache):
        super().__init__()
        self.cache = cache
        self.save_acquired_lock = False

    async def save(self, day, tokens):
        async with self.cache.lock(f"budget:{day.isoformat()}:write", ttl_s=10):
            self.save_acquired_lock = True
        self.tokens = max(self.tokens, tokens)


class ExpiredLeaseCache(MemoryCache):
    @asynccontextmanager
    async def lock(self, key, *, ttl_s):
        yield


class LeaseRaceUsage(Usage):
    def __init__(self):
        super().__init__()
        self.first_load_started = asyncio.Event()
        self.release_first_load = asyncio.Event()
        self.load_count = 0

    async def load(self, day):
        self.load_count += 1
        if self.load_count == 1:
            self.first_load_started.set()
            await self.release_first_load.wait()
        return 0


async def _admin_app(*, setup_users=(), moderators=(), admin_users=()):
    owner = user("owner")
    store = MemoryAdministrationStore()
    group = ChatRef("fake", "group")
    revision = 0
    for subject in setup_users:
        await store.apply(
            actor=owner.ref,
            owners=frozenset({owner.ref.key}),
            expected_revision=revision,
            change=AdminChange("grant", group, user=subject.ref),
        )
        revision += 1
    for subject in moderators:
        await store.apply(
            actor=owner.ref,
            owners=frozenset({owner.ref.key}),
            expected_revision=revision,
            change=AdminChange("moderator_grant", group, user=subject.ref),
        )
        revision += 1
    access = AccessPolicy(
        [group.key],
        [subject.ref.key for subject in admin_users],
        owners=[owner.ref.key],
        store=store,
    )
    limits = Limits(
        MemoryCache(),
        LimitsSettings(DAILY_TOKEN_BUDGET=2_000_000),
        budget_override=access.budget_override,
    )
    platform = FakePlatform()
    plugin = AdministrationPlugin(access, store, frozenset({owner.ref.key}), limits=limits)
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[plugin],
        bot_name="sbbot",
    )
    return owner, group, store, platform, app


def test_budget_defaults_override_and_day_scoped_extra():
    cache = MemoryCache()
    today = datetime(2026, 10, 31, 12, tzinfo=UTC)
    settings = LimitsSettings(DAILY_TOKEN_BUDGET=2_000_000, BUDGET_TZ="UTC")
    override = BudgetOverride(1_000_000, 200_000, date(2026, 10, 31), "tg:1", today)
    limits = Limits(cache, settings, budget_override=lambda: override)
    assert limits.base_budget() == 1_000_000
    assert limits.extra_today(today) == 200_000
    assert limits.daily_budget(today) == 1_200_000
    assert limits.extra_today(today + timedelta(days=1)) == 0
    assert limits.reset_at(today) == datetime(2026, 11, 1, tzinfo=UTC)


@pytest.mark.parametrize("value", [99_999, 100_000_001, True])
def test_invalid_daily_override_falls_back_to_env(value):
    limits = Limits(
        MemoryCache(),
        LimitsSettings(DAILY_TOKEN_BUDGET=2_000_000),
        budget_override=lambda: BudgetOverride(value, None, None, "tg:1", None),
    )
    assert limits.base_budget() == 2_000_000


def test_raising_override_falls_back_to_env():
    def fail():
        raise RuntimeError("fake failure")

    limits = Limits(
        MemoryCache(), LimitsSettings(DAILY_TOKEN_BUDGET=2_000_000), budget_override=fail
    )
    assert limits.base_budget() == 2_000_000


@pytest.mark.asyncio
async def test_budget_exhausted_and_check_use_override_cap():
    cache = MemoryCache()
    limits = Limits(
        cache,
        LimitsSettings(DAILY_TOKEN_BUDGET=100_000),
        budget_override=lambda: BudgetOverride(200_000, None, None, "tg:1", None),
    )
    await limits.record(150_000)
    assert not await limits.budget_exhausted()
    assert await limits.check(ChatRef("tg", "-1"), UserRef("tg", "1")) is None
    limits._budget_override = lambda: BudgetOverride(100_000, None, None, "tg:1", None)
    assert await limits.budget_exhausted()
    assert await limits.check(ChatRef("tg", "-1"), UserRef("tg", "1"))


@pytest.mark.asyncio
async def test_usage_mirror_restores_cache_and_record_continues_after_cache_loss():
    cache = MemoryCache()
    usage = Usage()
    limits = Limits(cache, LimitsSettings(BUDGET_TZ="UTC"), usage_store=usage)
    first = await limits.record(10)
    assert first == usage.tokens == 10
    await cache.delete(f"budget:{limits.day().isoformat()}")
    assert await limits.budget_used() == 10
    assert await cache.get(f"budget:{limits.day().isoformat()}") == b"10"
    await cache.delete(f"budget:{limits.day().isoformat()}")
    assert await limits.record(5) == 15
    assert usage.tokens == 15


@pytest.mark.asyncio
async def test_usage_mirror_failures_do_not_raise():
    cache = MemoryCache()
    limits = Limits(
        cache,
        LimitsSettings(BUDGET_TZ="UTC"),
        usage_store=Usage(fail_load=True, fail_save=True),
    )
    assert await limits.record(5) == 5
    assert await limits.budget_used() == 5
    await cache.delete(f"budget:{limits.day().isoformat()}")
    assert await limits.budget_used() == 0


@pytest.mark.asyncio
async def test_usage_restore_and_record_share_the_write_lock():
    cache = MemoryCache()
    usage = GatedUsage(100)
    limits = Limits(cache, LimitsSettings(BUDGET_TZ="UTC"), usage_store=usage)
    restore = asyncio.create_task(limits.budget_used())
    await usage.started.wait()
    record = asyncio.create_task(limits.record(50))
    await asyncio.sleep(0)
    usage.release.set()
    assert await restore == 100
    assert await record == 150
    assert await limits.budget_used() == 150


@pytest.mark.asyncio
async def test_mirror_io_times_out_without_stalling_budget_calls(caplog):
    cache = MemoryCache()
    limits = Limits(cache, LimitsSettings(BUDGET_TZ="UTC"), usage_store=HangingUsage())
    limits._MIRROR_TIMEOUT_S = 0.05
    assert await asyncio.wait_for(limits.record(5), timeout=0.5) == 5
    key = f"budget:{limits.day().isoformat()}"
    assert await cache.get(key) == b"5"
    await cache.delete(key)
    assert await asyncio.wait_for(limits.budget_used(), timeout=0.5) == 0
    assert caplog.text.count("error=TimeoutError") >= 3


@pytest.mark.asyncio
async def test_usage_save_runs_after_cache_lock_is_released():
    cache = MemoryCache()
    usage = LockCheckingUsage(cache)
    limits = Limits(cache, LimitsSettings(BUDGET_TZ="UTC"), usage_store=usage)
    assert await asyncio.wait_for(limits.record(5), timeout=0.5) == 5
    assert usage.save_acquired_lock


@pytest.mark.asyncio
async def test_delayed_record_cannot_lower_a_higher_total_after_lease_expiry():
    cache = ExpiredLeaseCache()
    usage = LeaseRaceUsage()
    limits = Limits(cache, LimitsSettings(BUDGET_TZ="UTC"), usage_store=usage)
    delayed = asyncio.create_task(limits.record(1))
    await usage.first_load_started.wait()
    higher = await limits.record(100)
    usage.release_first_load.set()
    delayed_result = await delayed
    key = f"budget:{limits.day().isoformat()}"
    assert higher == 100
    assert delayed_result >= higher
    assert int(await cache.get(key)) >= higher


@pytest.mark.asyncio
async def test_record_at_midnight_uses_one_instant_for_key_day_and_ttl():
    class TTLCache(MemoryCache):
        def __init__(self):
            super().__init__()
            self.writes = []

        async def set(self, key, value, *, ttl_s):
            self.writes.append((key, value, ttl_s))
            await super().set(key, value, ttl_s=ttl_s)

    cache = TTLCache()

    class RecordingUsage(Usage):
        def __init__(self):
            super().__init__()
            self.saved_days = []

        async def save(self, day, tokens):
            self.saved_days.append(day)
            await super().save(day, tokens)

    usage = RecordingUsage()
    limits = Limits(cache, LimitsSettings(BUDGET_TZ="UTC"), usage_store=usage)
    instant = datetime(2026, 11, 1, 0, 0, 0, 1000, tzinfo=UTC)
    assert await limits._record_at(7, instant) == 7
    assert await cache.get("budget:2026-10-31") is None
    assert await cache.get("budget:2026-11-01") == b"7"
    assert usage.saved_days == [date(2026, 11, 1)]
    assert cache.writes == [("budget:2026-11-01", b"7", 86_399)]


@pytest.mark.asyncio
async def test_budget_read_and_check_each_capture_the_clock_once():
    cache = MemoryCache()
    limits = Limits(cache, LimitsSettings(BUDGET_TZ="UTC"))
    instant = datetime(2026, 11, 1, 0, 0, 1, tzinfo=UTC)
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        return instant

    limits._now = clock
    await limits.record(1)
    assert calls == 1
    calls = 0
    await limits.budget_used()
    assert calls == 1
    calls = 0
    await limits.check(ChatRef("fake", "group"), UserRef("fake", "user"))
    assert calls == 1
    assert await cache.get("limit:chat:fake:group:2026-11-01") is not None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("100_000", 100_000),
        ("100,000", 100_000),
        ("1,000,000", 1_000_000),
        ("500k", 500_000),
        ("15m", 15_000_000),
        ("1.5m", 1_500_000),
    ],
)
def test_parse_budget_tokens(value, expected):
    assert _parse_tokens(value) == expected


@pytest.mark.parametrize("value", ["0", "-5", "abc", "true", "1.5m1"])
def test_reject_invalid_budget_tokens(value):
    with pytest.raises(ValueError):
        _parse_tokens(value)


@pytest.mark.asyncio
async def test_budget_store_is_global_and_owner_only_with_audit():
    owner, admin = UserRef("tg", "1"), UserRef("tg", "2")
    store = MemoryAdministrationStore()
    change = AdminChange("budget_set", global_scope("tg"), value=150_000)
    with pytest.raises(PermissionError):
        await store.apply(
            actor=admin, owners=frozenset({owner.key}), expected_revision=0, change=change
        )
    await store.apply(
        actor=owner, owners=frozenset({owner.key}), expected_revision=0, change=change
    )
    snapshot = await store.snapshot()
    assert snapshot.budget.daily_tokens == 150_000
    row = (await store.audit())[0]
    assert row.actor == owner.key and row.group == "tg:*" and row.detail == "budget=150000"
    access = AccessPolicy([], [], owners=[owner.key], store=store)
    await access.refresh()
    assert access.budget_override() == snapshot.budget


@pytest.mark.asyncio
async def test_admin_budget_requires_owner_and_applies_only_after_confirmation():
    owner, full_admin = user("owner"), user("admin")
    store = MemoryAdministrationStore()
    access = AccessPolicy([], [full_admin.ref.key], owners=[owner.ref.key], store=store)
    limits = Limits(
        MemoryCache(),
        LimitsSettings(DAILY_TOKEN_BUDGET=2_000_000),
        budget_override=access.budget_override,
    )
    platform = FakePlatform()
    admin_plugin = AdministrationPlugin(
        access,
        store,
        frozenset({owner.ref.key}),
        limits=limits,
    )
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[admin_plugin],
        bot_name="sbbot",
    )
    await app.handle(message("/admin usage set 150k", dm=True, sender=owner))
    assert (await store.snapshot()).budget is None
    pending = platform.sent[-1]
    await app.handle(press(pending.action_id, sender=owner, chat=pending.chat))
    assert (await store.snapshot()).budget.daily_tokens == 150_000
    current_day = limits.day
    await app.handle(message("/admin usage extra 10k", dm=True, sender=owner))
    stale_pending = platform.sent[-1]
    limits.day = lambda now=None: current_day(now) + timedelta(days=1)
    await app.handle(press(stale_pending.action_id, sender=owner, chat=stale_pending.chat))
    assert "Today's date changed" in platform.texts[-1]
    assert (await store.snapshot()).revision == 1
    limits.day = current_day

    await app.handle(message("/admin usage extra 10k", dm=True, sender=owner))
    extra_pending = platform.sent[-1]
    await app.handle(press(extra_pending.action_id, sender=owner, chat=extra_pending.chat))
    assert (await store.snapshot()).budget.extra_tokens == 10_000
    assert (await store.snapshot()).budget.extra_day == limits.day()
    assert access.budget_override() == (await store.snapshot()).budget
    assert limits.extra_today() == 10_000
    await app.handle(message("/admin usage", dm=True, sender=owner))
    assert "Used today:" in platform.texts[-1]
    status = platform.texts[-1]
    await app.handle(message("/admin budget", dm=True, sender=owner))
    assert platform.texts[-1] == status
    assert "Extra today: 10,000" in platform.texts[-1]
    assert "override by fake:owner" in platform.texts[-1]
    assert "Remaining:" in platform.texts[-1] and "Resets:" in platform.texts[-1]
    await app.handle(message("/admin usage extra clear", dm=True, sender=owner))
    clear_pending = platform.sent[-1]
    await app.handle(press(clear_pending.action_id, sender=owner, chat=clear_pending.chat))
    assert (await store.snapshot()).budget.extra_tokens is None
    await app.handle(message("/admin usage default", dm=True, sender=owner))
    reset_pending = platform.sent[-1]
    await app.handle(press(reset_pending.action_id, sender=owner, chat=reset_pending.chat))
    assert (await store.snapshot()).budget.daily_tokens is None
    sent = len(platform.sent)
    await app.handle(message("/admin usage set 200k", dm=True, sender=full_admin))
    assert len(platform.sent) == sent
    assert (await store.snapshot()).revision == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["setup", "moderator", "full_admin"])
async def test_nonowners_do_not_queue_budget_changes(role):
    actor = user("actor")
    kwargs = {
        "setup_users": (actor,) if role == "setup" else (),
        "moderators": (actor,) if role == "moderator" else (),
        "admin_users": (actor,) if role == "full_admin" else (),
    }
    _, _, store, platform, app = await _admin_app(**kwargs)
    commands = (
        "/admin usage set 150k",
        "/admin usage default",
        "/admin usage extra 10k",
        "/admin usage extra clear",
    )
    initial_sent = len(platform.sent)
    for command in commands:
        await app.handle(message(command, dm=True, sender=actor))
        if role == "full_admin":
            assert len(platform.sent) == initial_sent
        else:
            assert "Owner access is required" in platform.texts[-1]
            assert platform.sent[-1].action_id is None
    assert not any(item.action_id for item in platform.sent)
    assert (await store.snapshot()).revision == (1 if role != "full_admin" else 0)


@pytest.mark.asyncio
async def test_budget_command_visibility_and_group_dm_requirement():
    owner, group, store, platform, app = await _admin_app()
    await app.handle(message("/admin@sbbot budget set 150k", sender=owner))
    assert "private chat" in platform.texts[-1]
    assert platform.sent[-1].action_id is None
    assert (await store.snapshot()).revision == 0

    stranger = user("stranger")
    sent = len(platform.sent)
    await app.handle(message("/admin usage set 150k", dm=True, sender=stranger))
    assert len(platform.sent) == sent


@pytest.mark.asyncio
async def test_budget_command_bounds_and_valid_endpoints():
    owner, _, store, platform, app = await _admin_app()
    for value in ("99_999", "100_000_001", "0", "abc", "true", "-5", "1.5k"):
        await app.handle(message(f"/admin usage set {value}", dm=True, sender=owner))
        assert "Daily budget must be between 100,000 and 100,000,000 tokens." in platform.texts[-1]
        assert platform.sent[-1].action_id is None
        assert (await store.snapshot()).revision == 0
    for value in ("1.5k", "9_999", "100_000_001", "abc", "true", "-5"):
        await app.handle(message(f"/admin usage extra {value}", dm=True, sender=owner))
        assert (
            "Extra allowance must be between 10,000 and 100,000,000 tokens." in platform.texts[-1]
        )
        assert platform.sent[-1].action_id is None
        assert (await store.snapshot()).revision == 0
    for command in (
        "/admin usage set 100_000",
        "/admin usage set 100_000_000",
        "/admin usage extra 10_000",
    ):
        await app.handle(message(command, dm=True, sender=owner))
        queued = platform.sent[-1]
        assert queued.action_id is not None
        await app.handle(press(queued.action_id, sender=owner, chat=queued.chat))
    snapshot = await store.snapshot()
    assert snapshot.budget.daily_tokens == 100_000_000
    assert snapshot.budget.extra_tokens == 10_000


@pytest.mark.asyncio
async def test_budget_change_store_validation_enforces_scope_value_and_target():
    owner = user("owner").ref
    global_group = global_scope("fake")
    changes = (
        AdminChange("budget_set", global_group, value=99_999),
        AdminChange("budget_set", global_group, value=100_000_001),
        AdminChange("budget_set", global_group, value=True),
        AdminChange("budget_extra", global_group, value=9_999, day=date.today()),
        AdminChange("budget_set", global_group, user=UserRef("fake", "target"), value=100_000),
        AdminChange("budget_set", ChatRef("fake", "group"), value=100_000),
        AdminChange("budget_set", global_group, value=100_000, day=date.today()),
    )
    store = MemoryAdministrationStore()
    for change in changes:
        with pytest.raises(ValueError):
            validate_change(change)
        with pytest.raises(ValueError):
            await store.apply(
                actor=owner,
                owners=frozenset({owner.key}),
                expected_revision=0,
                change=change,
            )
    assert (await store.snapshot()).revision == 0


def _commit_context(actor, store, owners):
    access = AccessPolicy([], [actor.key], owners=owners, store=store)
    plugin = AdministrationPlugin(
        access,
        store,
        owners,
        limits=Limits(MemoryCache(), budget_override=access.budget_override),
    )
    platform = FakePlatform()
    ctx = CommandContext(
        ChatRef(actor.platform, actor.id),
        User(actor, actor.id),
        True,
        True,
        platform,
        memory_stores(),
    )
    return plugin, ctx


def _pending_action(actor, payload):
    chat = ChatRef(actor.platform, actor.id)
    return PendingAction.create(
        chat=chat,
        requester=actor,
        plugin="administration",
        kind="change",
        payload=payload,
        summary="Confirm budget change",
    )


@pytest.mark.asyncio
async def test_forged_nonowner_pending_budget_change_is_refused():
    owner, actor = user("owner").ref, user("admin").ref
    store = MemoryAdministrationStore()
    plugin, ctx = _commit_context(actor, store, frozenset({owner.key}))
    payload = {
        "operation": "budget_set",
        "group": global_scope("fake").key,
        "user": None,
        "value": 100_000,
        "expected_revision": 0,
    }
    reply = await plugin.commit(_pending_action(actor, payload), ctx)
    assert reply == "Owner access is required for this administration change."
    assert (await store.snapshot()).revision == 0


@pytest.mark.asyncio
async def test_budget_confirmation_stale_revision_does_not_apply_change():
    owner, _, store, platform, app = await _admin_app()
    await app.handle(message("/admin usage set 120k", dm=True, sender=owner))
    pending = platform.sent[-1]
    await store.apply(
        actor=owner.ref,
        owners=frozenset({owner.ref.key}),
        expected_revision=0,
        change=AdminChange("budget_set", global_scope("fake"), value=130_000),
    )
    await app.handle(press(pending.action_id, sender=owner, chat=pending.chat))
    assert "Settings changed since this request" in platform.texts[-1]
    snapshot = await store.snapshot()
    assert snapshot.revision == 1 and snapshot.budget.daily_tokens == 130_000


@pytest.mark.asyncio
async def test_budget_confirmation_rejects_unexpected_or_invalid_payload_keys():
    owner = user("owner").ref
    store = MemoryAdministrationStore()
    plugin, ctx = _commit_context(owner, store, frozenset({owner.key}))
    base = {
        "operation": "budget_set",
        "group": global_scope("fake").key,
        "user": None,
        "value": 100_000,
        "expected_revision": 0,
    }
    payloads = []
    extra_key = dict(base, unexpected=True)
    payloads.append(extra_key)
    missing_key = dict(base)
    del missing_key["value"]
    payloads.append(missing_key)
    payloads.append(dict(base, day=date.today().isoformat()))
    non_iso_day = dict(base, operation="budget_extra", value=10_000, day="not-a-date")
    payloads.append(non_iso_day)
    for payload in payloads:
        reply = await plugin.commit(_pending_action(owner, payload), ctx)
        assert reply == "That confirmation is invalid. Start the command again."
        assert (await store.snapshot()).revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "expected"), [(200_000, True), (100_000, False)])
async def test_reopened_conversation_compaction_uses_runtime_budget(cap, expected):
    stores = memory_stores()
    limits = Limits(
        stores.cache,
        LimitsSettings(DAILY_TOKEN_BUDGET=150_000, BUDGET_TZ="UTC"),
        budget_override=lambda: BudgetOverride(cap, None, None, "fake:owner", None),
    )
    await limits.record(150_000)

    class Store:
        checked = False

        async def unsummarized_conversation_turns(self, *_args):
            self.checked = True
            return []

    store = Store()
    conversation_plugin = ConversationPlugin(store=store, limits=limits, provider=object())
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([], []),
        plugins=[conversation_plugin],
        bot_name="sbbot",
        conversation_threads=ThreadSettings(),
    )
    now = datetime.now(UTC)
    await app._reopen_compact(
        SimpleNamespace(chat=ChatRef("fake", "group"), sent_at=now),
        SimpleNamespace(id="conversation"),
        now - timedelta(hours=2),
    )
    assert store.checked is expected


@pytest.mark.asyncio
async def test_budget_status_includes_injection_flag_counter():
    from chatcore.security.injection import counter_key

    owner, _group, _store, platform, app = await _admin_app()
    await app.stores.cache.incr(counter_key(), ttl_s=3600)
    await app.stores.cache.incr(counter_key(), ttl_s=3600)
    await app.handle(message("/admin usage", dm=True, sender=owner))
    assert "Injection flags today (UTC): 2" in platform.texts[-1]

    await app.stores.cache.delete(counter_key())
    await app.handle(message("/admin usage", dm=True, sender=owner))
    assert "Injection flags today (UTC): 0" in platform.texts[-1]
