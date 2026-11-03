from dataclasses import replace
from datetime import UTC, datetime, timedelta

from chatcore.events import ChatRef
from chatcore.limits import BudgetOverride, Limits, LimitsSettings
from chatcore.testing import memory_stores, user
from chatcore_research.jobs import MemoryResearchJobs
from chatcore_research.plugin import ResearchPlugin
from chatcore_research.quota import ResearchQuota
from chatcore_research.settings import ResearchSettings
from chatcore_research.worker import ResearchWorker


async def test_daily_caps_and_next_day_reset():
    stores = memory_stores()
    limits = Limits(stores.cache, LimitsSettings(DAILY_TOKEN_BUDGET=1000))
    limits._day = lambda now=None: "2026-10-31"
    settings = ResearchSettings(per_user_per_day=1, per_chat_per_day=2)
    quota = ResearchQuota(stores.cache, limits, settings)
    person = user().ref
    chat = ChatRef("fake", "group")

    assert await quota.check_and_count(chat, person) is None
    assert "research limit (1)" in await quota.check_and_count(chat, person)
    limits._day = lambda now=None: "2026-11-01"
    assert await quota.check_and_count(chat, person) is None


async def test_research_quota_and_worker_budget_use_runtime_override():
    stores = memory_stores()
    limits = Limits(
        stores.cache,
        LimitsSettings(DAILY_TOKEN_BUDGET=100_000, BUDGET_TZ="UTC"),
        budget_override=lambda: BudgetOverride(200_000, None, None, "fake:owner", None),
    )
    await limits.record(150_000)
    quota = ResearchQuota(stores.cache, limits, ResearchSettings())
    assert await quota.check_and_count(ChatRef("fake", "group"), user().ref) is None

    class LimitsHolder:
        pass

    holder = LimitsHolder()
    holder.limits = limits
    assert await ResearchPlugin._budget_ok(holder)


async def test_chat_daily_cap_is_shared_across_people():
    stores = memory_stores()
    limits = Limits(stores.cache, LimitsSettings(DAILY_TOKEN_BUDGET=1000))
    limits._day = lambda now=None: "2026-10-31"
    quota = ResearchQuota(
        stores.cache,
        limits,
        ResearchSettings(per_user_per_day=10, per_chat_per_day=1),
    )
    chat = ChatRef("fake", "group")

    assert await quota.check_and_count(chat, user("alice").ref) is None
    assert "chat has used today's research limit" in await quota.check_and_count(
        chat, user("bob").ref
    )
    limits._day = lambda now=None: "2026-11-01"
    assert await quota.check_and_count(chat, user("bob").ref) is None


async def test_background_job_scope_cancel_and_reserved_send_not_replayed():
    now = datetime(2026, 10, 31, tzinfo=UTC)
    jobs = MemoryResearchJobs(clock=lambda: now)
    alice = user("alice").ref
    bob = user("bob").ref
    chat_a = ChatRef("fake", "a")
    chat_b = ChatRef("fake", "b")
    job_a = await jobs.create(chat_a, alice, "thread-1", "private topic", (), is_dm=False)
    assert job_a is not None
    assert await jobs.create(chat_b, alice, None, "second", (), is_dm=False) is None
    bob_job = await jobs.create(chat_a, bob, None, "bob topic", (), is_dm=False)
    assert bob_job is not None
    assert await jobs.list_for(chat_b.key, alice.key) == []
    assert await jobs.cancel(chat_b.key, alice.key) is None
    assert (await jobs.cancel(chat_a.key, bob.key)).id == bob_job.id

    claimed = await jobs.claim(now, 10, 2)
    assert claimed.id == job_a.id
    assert await jobs.reserve_delivery(claimed)
    assert await jobs.claim(now + timedelta(seconds=11), 10, 2) is None
    terminal = jobs.rows[job_a.id]
    assert terminal.status == "completed"
    assert terminal.delivery_state == "unknown"


async def test_inline_slot_release_only_removes_its_own_token():
    stores = memory_stores()
    settings = ResearchSettings()
    quota = ResearchQuota(stores.cache, Limits(stores.cache), settings)
    person = user().ref

    token = await quota.acquire_inline(person)
    assert token is not None
    assert await quota.acquire_inline(person) is None
    await quota.release_inline(person, token)
    assert await quota.acquire_inline(person) is not None


async def test_inline_slot_ttl_covers_tool_run_and_stale_release_keeps_new_token():
    stores = memory_stores()

    class RecordingCache:
        def __init__(self, inner):
            self.inner = inner
            self.ttls = []

        async def get(self, key):
            return await self.inner.get(key)

        async def set(self, key, value, *, ttl_s):
            self.ttls.append(ttl_s)
            await self.inner.set(key, value, ttl_s=ttl_s)

        async def delete(self, key):
            await self.inner.delete(key)

    cache = RecordingCache(stores.cache)
    settings = ResearchSettings(inline_timeout_s=10, tool_timeout_s=55)
    quota = ResearchQuota(cache, Limits(stores.cache), settings)
    person = user("ttl-owner").ref
    key = f"research:active:{person.key}"

    stale = await quota.acquire_inline(person)
    assert stale is not None
    assert cache.ttls[-1] >= 85
    await cache.delete(key)
    current = await quota.acquire_inline(person)
    assert current is not None and current != stale

    await quota.release_inline(person, stale)

    assert await cache.get(key) == current.encode()


async def test_purges_scope_user_chat_and_terminal_retention():
    now = datetime(2026, 10, 31, tzinfo=UTC)
    jobs = MemoryResearchJobs(clock=lambda: now)
    alice, bob = user("alice").ref, user("bob").ref
    chat_a, chat_b = ChatRef("fake", "a"), ChatRef("fake", "b")
    await jobs.create(chat_a, alice, None, "a", (), is_dm=False)
    bob_a = await jobs.create(chat_a, bob, None, "b", (), is_dm=False)
    assert await jobs.purge_user(chat_a.key, alice.key) == 1
    alice_b = await jobs.create(chat_b, alice, None, "c", (), is_dm=False)
    assert await jobs.active_for(alice.key) == alice_b
    assert await jobs.purge_chat(chat_b.key) == 1
    assert await jobs.active_for(alice.key) is None
    assert await jobs.active_for(bob.key) == bob_a

    cancelled = await jobs.cancel(chat_a.key, bob.key)
    jobs.rows[cancelled.id] = replace(jobs.rows[cancelled.id], finished_at=now - timedelta(days=40))
    active = await jobs.create(chat_a, alice, None, "active", (), is_dm=False)
    assert await jobs.purge_older_than(now - timedelta(days=30)) == 1
    assert active.id in jobs.rows


async def test_expired_attempt_limit_marks_failure_for_one_notice():
    now = datetime(2026, 10, 31, tzinfo=UTC)
    jobs = MemoryResearchJobs(clock=lambda: now)
    job = await jobs.create(ChatRef("fake", "a"), user().ref, None, "topic", (), is_dm=False)
    first = await jobs.claim(now, 1, 1)
    failed = await jobs.claim(now + timedelta(seconds=2), 1, 1)

    assert first.id == job.id
    assert failed.id == job.id
    assert failed.status == "failed"
    assert failed.error_class == "AttemptsExhausted"
    assert await jobs.claim(now + timedelta(seconds=4), 1, 1) is None


async def test_release_is_fenced_and_restores_attempt_count_with_explicit_dm_scope():
    now = datetime(2026, 10, 31, tzinfo=UTC)
    jobs = MemoryResearchJobs(clock=lambda: now)
    chat, owner = ChatRef("fake", "not-the-user-key"), user("dm-owner").ref
    created = await jobs.create(chat, owner, "thread", "topic", (), is_dm=True)
    claimed = await jobs.claim(now, 30, 2)

    assert created.is_dm is True
    assert await jobs.release(claimed)
    released = jobs.rows[created.id]
    assert released.status == "queued"
    assert released.attempts == 0
    assert released.lease_token is None
    assert not await jobs.finish(claimed, status="failed", delivery_state=None, error_class="Stale")
    reclaimed = await jobs.claim(now, 30, 2)
    assert reclaimed.attempts == 1
    assert await jobs.finish(reclaimed, status="completed", delivery_state="sent")


async def test_worker_does_not_claim_during_budget_pause_and_sleeps_each_poll():
    now = datetime(2026, 10, 31, tzinfo=UTC)

    class CountingJobs(MemoryResearchJobs):
        def __init__(self):
            super().__init__(clock=lambda: now)
            self.claim_calls = 0

        async def claim(self, *args):
            self.claim_calls += 1
            return await super().claim(*args)

    jobs = CountingJobs()
    await jobs.create(
        ChatRef("fake", "paused"), user("paused-owner").ref, None, "topic", (), is_dm=False
    )
    sleep_calls = 0
    worker = None

    async def sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 3:
            worker._closing = True

    async def can_claim():
        return False

    worker = ResearchWorker(
        jobs,
        lambda _job: None,
        settings=ResearchSettings(poll_seconds=0.1),
        sleep=sleep,
        can_claim=can_claim,
    )
    await worker._poll()

    assert jobs.claim_calls == 0
    assert sleep_calls == 3
