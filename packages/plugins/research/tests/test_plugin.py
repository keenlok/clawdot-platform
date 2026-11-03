import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from chatcore.access import AccessPolicy
from chatcore.display_time_store import display_service
from chatcore.events import ChatRef
from chatcore.limits import Limits
from chatcore.llm.base import ProviderError
from chatcore.llm.types import Completion, Message, ToolCall, Usage
from chatcore.plugin import CommandContext
from chatcore.testing import FakePlatform, ScriptedProvider, memory_stores, message, user
from chatcore.tools.registry import ToolContext
from chatcore_research.agent import ResearchReport, ResearchTimeout, Source
from chatcore_research.jobs import MemoryResearchJobs
from chatcore_research.plugin import ResearchPlugin
from chatcore_research.quota import ResearchQuota
from chatcore_research.settings import ResearchSettings
from chatcore_research.worker import ResearchWorker
from chatcore_web import WebSettings


class DenyResearch:
    async def can_research(self, chat, person):
        return False


class RevokeAfterStart:
    def __init__(self):
        self.calls = 0

    async def can_research(self, chat, person):
        self.calls += 1
        return self.calls == 1


class NeverProvider:
    name = "never"

    async def complete(self, *args, **kwargs):
        raise AssertionError("denied research must not call the provider")


async def test_access_seam_refuses_start_and_suppresses_queued_job_delivery():
    platform = FakePlatform()
    stores = memory_stores()
    access = AccessPolicy(["fake:group"], [])
    jobs = MemoryResearchJobs()
    sent = []

    async def sender(chat, text, thread):
        sent.append((chat, text, thread))

    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=access,
        platform=platform,
        sender=sender,
        jobs=jobs,
        research_access=DenyResearch(),
        bot_name="test_bot",
    )
    incoming = message("/research topic")
    ctx = CommandContext(
        incoming.chat,
        incoming.user,
        False,
        False,
        platform,
        stores,
        message=incoming,
    )

    await plugin.commands()[0].handler(ctx, "topic")
    tool_ctx = ToolContext(incoming.chat, incoming.user, False, platform, stores)
    result = await plugin.tools()[0].handler(tool_ctx, topic="topic")
    queued = await jobs.create(
        incoming.chat, incoming.user.ref, None, "queued topic", (), is_dm=False
    )
    claimed = await jobs.claim(datetime.now(UTC), 60, 2)
    plugin.stores = stores
    await plugin._run_job(claimed)

    assert "Research isn't available" in platform.texts[0]
    assert result.is_error and "Research isn't available" in result.content
    assert jobs.rows[queued.id].status == "cancelled"
    assert jobs.rows[queued.id].delivery_state == "suppressed"
    assert sent == []


async def test_real_agent_timeout_queues_same_job_without_counting_quota_twice():
    platform = FakePlatform()
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    started = asyncio.Event()

    class BlockingProvider:
        async def complete(self, *_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

    settings = ResearchSettings()
    settings.inline_timeout_s = 0.01
    plugin = ResearchPlugin(
        settings,
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=BlockingProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=lambda *_args: None,
        jobs=jobs,
        bot_name="test_bot",
    )

    incoming = message("/research topic", thread_id="thread-2")

    await plugin._handle_text(incoming, "topic", stores)

    assert started.is_set()
    job = await jobs.active_for(incoming.user.ref.key)
    count = await stores.cache.get(f"research:user:{incoming.user.ref.key}:{plugin.limits._day()}")
    assert job.topic == "topic"
    assert job.chat == incoming.chat
    assert job.thread_id == "thread-2"
    assert count == b"1"
    assert await stores.cache.get(f"research:active:{incoming.user.ref.key}") is None
    assert any("I'll post the report here" in text for text in platform.texts)


async def test_daily_global_token_budget_blocks_research_before_counting():
    stores = memory_stores()
    limits = Limits(stores.cache)
    await limits.record(limits.settings.DAILY_TOKEN_BUDGET)
    quota = ResearchQuota(stores.cache, limits, ResearchSettings())

    refusal = await quota.check_and_count(message("x").chat, message("x").user.ref)

    assert "token budget" in refusal
    assert (
        await stores.cache.get(f"research:user:{message('x').user.ref.key}:{limits._day()}") is None
    )


async def test_status_sanitizes_urls_from_job_topic():
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    incoming = message("/research status", sender=user("topic-status"))
    await jobs.create(
        incoming.chat,
        incoming.user.ref,
        None,
        "https://evil.example/?secret=1 prices",
        (),
        is_dm=False,
    )
    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=FakePlatform(),
        sender=lambda *_args: None,
        jobs=jobs,
        bot_name="test_bot",
    )

    status = await plugin._status(incoming, stores)

    assert "[link removed] prices" in status
    assert "https://evil.example" not in status


async def test_research_status_formats_created_at_in_effective_display_zone():
    fixed = datetime(2026, 11, 1, 1, tzinfo=UTC)
    stores = memory_stores()
    jobs = MemoryResearchJobs(clock=lambda: fixed)
    incoming = message("/research status", sender=user("created-time"))
    await jobs.create(incoming.chat, incoming.user.ref, None, "topic", (), is_dm=False)
    await display_service(stores).set_zone(
        incoming.chat, incoming.user.ref, is_dm=False, zone="Asia/Singapore"
    )
    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=FakePlatform(),
        sender=lambda *_args: None,
        jobs=jobs,
        bot_name="test_bot",
    )

    status = await plugin._status(incoming, stores)

    assert "2026-11-01 09:00:00 Asia/Singapore (UTC+08:00)" in status


async def test_initial_access_exception_releases_inline_slot():
    class RaisingResearchAccess:
        async def can_research(self, *_args):
            raise RuntimeError("permission store unavailable")

    platform = FakePlatform()
    stores = memory_stores()
    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=lambda *_args: None,
        research_access=RaisingResearchAccess(),
        bot_name="test_bot",
    )
    incoming = message("/research topic")

    with pytest.raises(RuntimeError, match="permission store unavailable"):
        await plugin._handle_text(incoming, "topic", stores)

    assert await stores.cache.get(f"research:active:{incoming.user.ref.key}") is None
    assert plugin._inline_chats == {}


async def test_outer_tool_deadline_queues_background_and_releases_slot():
    platform = FakePlatform()
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    started = asyncio.Event()

    class BlockingProvider:
        async def complete(self, *_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=BlockingProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=lambda *_args: None,
        jobs=jobs,
        bot_name="test_bot",
    )
    incoming = message("tool topic")
    ctx = ToolContext(incoming.chat, incoming.user, False, platform, stores)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await plugin.tools()[0].handler(ctx, topic="tool topic")

    queued = await jobs.active_for(incoming.user.ref.key)
    assert started.is_set()
    assert queued is not None and queued.topic == "tool topic"
    assert await stores.cache.get(f"research:active:{incoming.user.ref.key}") is None
    assert any(text == "Research continues in the background." for _, text in platform.edits)


async def test_aclose_cancels_blocking_command_research():
    platform = FakePlatform()
    stores = memory_stores()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingProvider:
        async def complete(self, *_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=BlockingProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=lambda *_args: None,
        bot_name="test_bot",
    )
    incoming = message("/research shutdown topic")
    run = asyncio.create_task(plugin._handle_text(incoming, "shutdown topic", stores))
    await started.wait()

    await plugin.aclose()
    result = await asyncio.gather(run, return_exceptions=True)

    assert run.done()
    assert isinstance(result[0], asyncio.CancelledError)
    assert cancelled.is_set()
    assert await stores.cache.get(f"research:active:{incoming.user.ref.key}") is None
    assert plugin._tasks == set()


async def test_status_and_cancel_are_limited_to_owner_and_current_chat():
    platform = FakePlatform()
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=lambda *_args: None,
        jobs=jobs,
        bot_name="test_bot",
    )
    alice, bob = user("alice"), user("bob")
    chat_a, chat_b = message("x").chat, ChatRef("fake", "other")
    alice_job = await jobs.create(chat_a, alice.ref, None, "alice secret", (), is_dm=False)
    bob_job = await jobs.create(chat_a, bob.ref, None, "bob secret", (), is_dm=False)
    assert alice_job and bob_job

    await plugin._handle_text(
        message("/research status", sender=alice, chat=chat_a), "status", stores
    )
    status = platform.texts[-1]
    await plugin._handle_text(
        message("/research cancel", sender=alice, chat=chat_b), "cancel", stores
    )
    other_chat = platform.texts[-1]

    assert "alice secret" in status
    assert "bob secret" not in status
    assert other_chat == "You have no active research in this chat."
    assert jobs.rows[alice_job.id].status == "queued"
    assert jobs.rows[bob_job.id].status == "queued"
    await jobs.purge_user(chat_a.key, alice.ref.key)
    await stores.cache.set(f"research:active:{alice.ref.key}", b"opaque-token", ttl_s=30)
    refusal = await plugin._start_check(chat_b, alice.ref, count_quota=False)
    assert refusal == "You already have research running in another chat."


async def test_access_revoked_before_delivery_suppresses_completed_report(monkeypatch):
    platform = FakePlatform()
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    access_gate = RevokeAfterStart()
    sent = []

    async def sender(chat, text, thread):
        sent.append((chat, text, thread))

    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=sender,
        jobs=jobs,
        research_access=access_gate,
        bot_name="test_bot",
    )

    class CompletedResearch:
        async def run(self, *_args, **_kwargs):
            return ResearchReport("topic", "finding", (), Usage(), True)

    monkeypatch.setattr(plugin, "_agent", lambda *_args, **_kwargs: CompletedResearch())
    created = await jobs.create(message("x").chat, user().ref, "thread", "topic", (), is_dm=False)
    claimed = await jobs.claim(datetime.now(UTC), 60, 2)
    plugin.stores = stores

    await plugin._run_job(claimed)

    final = jobs.rows[created.id]
    assert access_gate.calls == 2
    assert final.status == "cancelled"
    assert final.delivery_state == "suppressed"
    assert sent == []


async def test_worker_unknown_send_is_completed_and_never_replayed(monkeypatch):
    platform = FakePlatform()
    stores = memory_stores()
    jobs = MemoryResearchJobs()

    async def failed_sender(*_args):
        raise RuntimeError("platform response unknown")

    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=failed_sender,
        jobs=jobs,
        bot_name="test_bot",
    )

    class CompletedResearch:
        async def run(self, *_args, **_kwargs):
            return ResearchReport("topic", "finding", (), Usage(), True)

    monkeypatch.setattr(plugin, "_agent", lambda *_args, **_kwargs: CompletedResearch())
    created = await jobs.create(message("x").chat, user().ref, "thread", "topic", (), is_dm=False)
    claimed = await jobs.claim(datetime.now(UTC), 1, 2)
    plugin.stores = stores

    await plugin._run_job(claimed)

    final = jobs.rows[created.id]
    assert final.status == "completed"
    assert final.delivery_state == "unknown"
    assert await jobs.claim(datetime.now(UTC) + timedelta(seconds=2), 1, 2) is None


async def test_research_tool_returns_untrusted_report_without_posting_it(monkeypatch):
    platform = FakePlatform()
    stores = memory_stores()
    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=lambda *_args: None,
        bot_name="test_bot",
    )

    class CompletedResearch:
        async def run(self, *_args, **_kwargs):
            return ResearchReport(
                "complex question",
                "Finding. [1]",
                (Source(1, "https://example.com/verified-source", "Source"),),
                Usage(),
                True,
            )

    monkeypatch.setattr(plugin, "_agent", lambda *_args, **_kwargs: CompletedResearch())
    ctx = ToolContext(message("user request").chat, user(), False, platform, stores)

    result = await plugin.tools()[0].handler(ctx, topic="complex question")

    assert result.untrusted
    assert "https://example.com/verified-source" in ctx.turn.seen_urls
    assert "https://example.com/verified-source" not in ctx.turn.human_urls
    assert result.content.startswith("Research: complex question")
    assert len(platform.sent) == 1
    assert platform.sent[0].text == "Researching…"
    assert any(text == "Research done." for _, text in platform.edits)


async def test_cancelled_background_run_stops_and_worker_claims_next_job(monkeypatch):
    platform = FakePlatform()
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    cancel_signal = asyncio.Event()
    started = asyncio.Event()
    delivered_second = asyncio.Event()
    sent = []

    async def fake_sleep(_seconds):
        if cancel_signal.is_set():
            await asyncio.sleep(3600)
        else:
            await cancel_signal.wait()

    async def sender(chat, text, thread):
        sent.append((chat, text, thread))

    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=sender,
        jobs=jobs,
        bot_name="test_bot",
        sleep=fake_sleep,
        lease_check_interval_s=0.01,
    )

    class PausedResearch:
        async def run(self, topic, *_args, **_kwargs):
            if topic == "first topic":
                started.set()
                await asyncio.Event().wait()
            return ResearchReport(topic, "finding", (), Usage(), True)

    monkeypatch.setattr(plugin, "_agent", lambda *_args, **_kwargs: PausedResearch())
    alice, bob = user("alice"), user("bob")
    chat = message("x").chat
    created = await jobs.create(chat, alice.ref, "thread", "first topic", (), is_dm=False)
    second = await jobs.create(chat, bob.ref, "thread-2", "second topic", (), is_dm=False)
    plugin.stores = stores
    worker = ResearchWorker(
        jobs,
        plugin._run_job,
        settings=ResearchSettings(poll_seconds=0.1),
    )
    worker.start()
    await started.wait()
    await jobs.cancel(created.chat.key, created.owner.key)
    cancel_signal.set()
    for _ in range(100):
        if jobs.rows[second.id].status == "completed":
            delivered_second.set()
            break
        await asyncio.sleep(0.01)
    await worker.aclose()

    assert delivered_second.is_set()
    assert jobs.rows[created.id].status == "cancelled"
    assert jobs.rows[second.id].status == "completed"
    assert [(chat_key, thread) for chat_key, _, thread in sent] == [(chat.key, "thread-2")]


async def test_exhausted_global_budget_requeues_claim_without_provider_call():
    platform = FakePlatform()
    stores = memory_stores()
    limits = Limits(stores.cache)
    jobs = MemoryResearchJobs()
    sent = []

    async def sender(chat, text, thread):
        sent.append((chat, text, thread))

    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=limits,
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=sender,
        jobs=jobs,
        bot_name="test_bot",
    )
    chat, owner = ChatRef("fake", "group"), user("budget-owner").ref
    created = await jobs.create(chat, owner, "thread", "budget topic", (), is_dm=False)
    claimed = await jobs.claim(datetime.now(UTC), 60, 2)
    await limits.record(limits.settings.DAILY_TOKEN_BUDGET)

    await plugin._run_job(claimed)

    current = jobs.rows[created.id]
    assert current.status == "queued"
    assert current.attempts == 0
    assert not sent


async def test_inline_and_background_start_are_serialized_per_user(monkeypatch):
    platform = FakePlatform()
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider:
        async def complete(self, *_args, **_kwargs):
            entered.set()
            await release.wait()
            from chatcore.llm.types import Completion, Message

            return Completion(Message.assistant("Done."), "end_turn")

    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=BlockingProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:a", "fake:b", "fake:c"], []),
        platform=platform,
        sender=lambda *_args: None,
        jobs=jobs,
        bot_name="test_bot",
        typing_interval_s=0.001,
    )
    alice = user("race-owner")
    incoming_inline = message("inline topic", sender=alice, chat=ChatRef("fake", "a"))
    incoming_long = message("--long queued topic", sender=alice, chat=ChatRef("fake", "b"))
    inline = asyncio.create_task(plugin._handle_text(incoming_inline, "inline topic", stores))
    background = asyncio.create_task(
        plugin._handle_text(incoming_long, "--long queued topic", stores)
    )
    await entered.wait()
    await plugin._handle_text(
        message("second inline", sender=alice, chat=ChatRef("fake", "c")),
        "second inline",
        stores,
    )
    await asyncio.gather(background)
    release.set()
    await inline

    assert await jobs.active_for(alice.ref.key) is None
    assert sum("already have research running" in text for text in platform.texts) == 2


async def test_inline_access_is_rechecked_before_command_and_tool_delivery(monkeypatch):
    for tool_path in (False, True):
        platform = FakePlatform()
        stores = memory_stores()
        gate = RevokeAfterStart()
        plugin = ResearchPlugin(
            ResearchSettings(),
            web_settings=WebSettings(tavily_api_key="fake-key"),
            provider=NeverProvider(),
            limits=Limits(stores.cache),
            access=AccessPolicy(["fake:group"], []),
            platform=platform,
            sender=lambda *_args: None,
            research_access=gate,
            bot_name="test_bot",
        )

        class CompletedResearch:
            async def run(self, *_args, **_kwargs):
                return ResearchReport("topic", "secret findings", (), Usage(), True)

        monkeypatch.setattr(plugin, "_agent", lambda *_args, **_kwargs: CompletedResearch())
        incoming = message("/research topic")
        if tool_path:
            ctx = ToolContext(incoming.chat, incoming.user, False, platform, stores)
            result = await plugin.tools()[0].handler(ctx, topic="topic")
            assert result.is_error
            assert "Research isn't available" in result.content
        else:
            await plugin._handle_text(incoming, "topic", stores)
            assert "Research isn't available" in platform.texts[-1]
        assert not any("secret findings" in text for text in platform.texts)


async def test_authorization_refresh_failure_fails_closed(caplog):
    class FailedRefreshAccess(AccessPolicy):
        async def refresh(self):
            raise RuntimeError("private store error")

    stores = memory_stores()
    jobs = MemoryResearchJobs()
    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=FailedRefreshAccess(["fake:group"], []),
        platform=FakePlatform(),
        sender=lambda *_args: None,
        jobs=jobs,
        bot_name="test_bot",
    )
    queued = await jobs.create(ChatRef("fake", "group"), user().ref, None, "topic", (), is_dm=False)
    claimed = await jobs.claim(datetime.now(UTC), 60, 2)

    assert queued.id == claimed.id
    with caplog.at_level(logging.WARNING):
        assert not await plugin._authorized(claimed)
    assert "RuntimeError" in caplog.text
    assert "private store error" not in caplog.text


async def test_cancelled_provider_failure_does_not_send_failure_notice(monkeypatch):
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    sent = []
    chat, owner = ChatRef("fake", "group"), user("cancelled-owner").ref

    async def sender(*args):
        sent.append(args)

    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=FakePlatform(),
        sender=sender,
        jobs=jobs,
        bot_name="test_bot",
    )

    class FailsAfterCancel:
        async def run(self, *_args, **_kwargs):
            await jobs.cancel(chat.key, owner.key)
            raise ProviderError("don't expose this")

    monkeypatch.setattr(plugin, "_agent", lambda *_args, **_kwargs: FailsAfterCancel())
    created = await jobs.create(chat, owner, "thread", "topic", (), is_dm=False)
    claimed = await jobs.claim(datetime.now(UTC), 60, 2)
    plugin.stores = stores

    await plugin._run_job(claimed)

    assert jobs.rows[created.id].status == "cancelled"
    assert sent == []


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ResearchTimeout(Usage()), "(time limit)"),
        (ProviderError("secret"), "(the language model was unavailable)"),
        (RuntimeError("secret"), "(an internal error)"),
    ],
)
def test_background_failure_notice_is_cause_specific(error, reason):
    assert ResearchPlugin._failure_notice(error) == f"Research couldn't be completed {reason}."


async def test_token_ceiling_notice_is_sent_only_after_fenced_finish():
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    sent = []

    async def sender(*args):
        sent.append(args)

    settings = ResearchSettings(long_token_ceiling=10000)
    plugin = ResearchPlugin(
        settings,
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=FakePlatform(),
        sender=sender,
        jobs=jobs,
        bot_name="test_bot",
    )
    chat, owner = ChatRef("fake", "group"), user("token-owner").ref
    created = await jobs.create(chat, owner, None, "topic", (), is_dm=False)
    claimed = await jobs.claim(datetime.now(UTC), 60, 2)
    exhausted = replace(claimed, tokens_used=settings.long_token_ceiling)
    plugin.stores = stores

    await plugin._run_job(exhausted)

    assert jobs.rows[created.id].status == "failed"
    assert len(sent) == 1
    assert sent[0][1] == "Research couldn't be completed (token limit)."


async def test_stale_token_ceiling_finish_does_not_send_notice(monkeypatch):
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    sent = []

    async def sender(*args):
        sent.append(args)

    settings = ResearchSettings(long_token_ceiling=10000)
    plugin = ResearchPlugin(
        settings,
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=FakePlatform(),
        sender=sender,
        jobs=jobs,
        bot_name="test_bot",
    )
    created = await jobs.create(
        ChatRef("fake", "group"), user("stale-token-owner").ref, None, "topic", (), is_dm=False
    )
    claimed = await jobs.claim(datetime.now(UTC), 60, 2)
    exhausted = replace(claimed, tokens_used=settings.long_token_ceiling)
    monkeypatch.setattr(jobs, "finish", AsyncMock(return_value=False))

    await plugin._run_job(exhausted)

    assert jobs.rows[created.id].status == "running"
    assert sent == []


async def test_queued_job_blocks_inline_and_second_long_without_leaking_topic():
    platform = FakePlatform()
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:a", "fake:b"], []),
        platform=platform,
        sender=lambda *_args: None,
        jobs=jobs,
        bot_name="test_bot",
    )
    alice = user("active-owner")
    chat_b = ChatRef("fake", "b")
    await plugin._handle_text(
        message("--long private topic", sender=alice, chat=chat_b),
        "--long private topic",
        stores,
    )
    await plugin._handle_text(
        message("inline request", sender=alice, chat=ChatRef("fake", "a")),
        "inline request",
        stores,
    )
    inline_refusal = platform.texts[-1]
    await plugin._handle_text(
        message("--long another private topic", sender=alice, chat=ChatRef("fake", "a")),
        "--long another private topic",
        stores,
    )
    long_refusal = platform.texts[-1]

    assert "another chat" in inline_refusal
    assert "another chat" in long_refusal
    assert "private topic" not in inline_refusal
    assert "private topic" not in long_refusal
    assert len(jobs.rows) == 1


async def test_worker_delivers_each_job_only_to_its_own_chat_and_thread(monkeypatch):
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    sent = []

    async def sender(chat, text, thread):
        sent.append((chat, text, thread))
        return SimpleNamespace(message_id=f"message-{len(sent)}")

    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=NeverProvider(),
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:a", "fake:b"], []),
        platform=FakePlatform(),
        sender=sender,
        jobs=jobs,
        bot_name="test_bot",
    )

    class TopicResearch:
        async def run(self, topic, *_args, **_kwargs):
            return ResearchReport(topic, f"finding for {topic}", (), Usage(), True)

    monkeypatch.setattr(plugin, "_agent", lambda *_args, **_kwargs: TopicResearch())
    alice, bob = user("delivery-a"), user("delivery-b")
    chat_a, chat_b = ChatRef("fake", "a"), ChatRef("fake", "b")
    a = await jobs.create(chat_a, alice.ref, "thread-a", "topic-a", (), is_dm=False)
    b = await jobs.create(chat_b, bob.ref, "thread-b", "topic-b", (), is_dm=False)
    plugin.stores = stores

    for _ in range(2):
        claimed = await jobs.claim(datetime.now(UTC), 60, 2)
        await plugin._run_job(claimed)

    assert {(chat, thread) for chat, _, thread in sent} == {
        (chat_a.key, "thread-a"),
        (chat_b.key, "thread-b"),
    }
    assert all(
        (chat == chat_a.key) == text.startswith("Research: topic-a") for chat, text, _ in sent
    )
    assert jobs.rows[a.id].status == jobs.rows[b.id].status == "completed"


@respx.mock
async def test_inline_and_background_logs_exclude_topics_pages_reports_and_full_urls(
    monkeypatch, caplog
):
    page_url = "https://host.example/private/path?secret=1"
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"title": "Title", "url": page_url, "content": "snippet"}]},
        )
    )
    safe_get = AsyncMock(
        return_value=SimpleNamespace(
            status=200,
            final_url=page_url,
            content_type="text/plain",
            body=b"page-secret",
            truncated=False,
        )
    )
    monkeypatch.setattr("chatcore_web.plugin.safe_get", safe_get)
    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant("", (ToolCall("1", "search_web", {"query": "find"}),)),
                "tool_use",
            ),
            Completion(
                Message.assistant("", (ToolCall("2", "fetch_web_page", {"url": page_url}),)),
                "tool_use",
            ),
            Completion(Message.assistant("report-secret [S1]"), "end_turn"),
            Completion(
                Message.assistant("", (ToolCall("4", "search_web", {"query": "find"}),)),
                "tool_use",
            ),
            Completion(
                Message.assistant("", (ToolCall("5", "fetch_web_page", {"url": page_url}),)),
                "tool_use",
            ),
            Completion(Message.assistant("background-report-secret [S1]"), "end_turn"),
        ]
    )
    platform = FakePlatform()
    stores = memory_stores()
    jobs = MemoryResearchJobs()
    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=provider,
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=lambda *_args: None,
        jobs=jobs,
        bot_name="test_bot",
    )
    owner = user("log-owner")
    inline = message("inline-topic-secret", sender=owner)
    background = message("--long background-topic-secret", sender=owner)
    with caplog.at_level(logging.DEBUG):
        await plugin._handle_text(inline, "inline-topic-secret", stores)
        await plugin._handle_text(background, "--long background-topic-secret", stores)
        claimed = await jobs.claim(datetime.now(UTC), 60, 2)
        plugin.stores = stores
        await plugin._run_job(claimed)

    for secret in (
        "inline-topic-secret",
        "background-topic-secret",
        "page-secret",
        "report-secret",
        "https://host.example/private/path",
    ):
        assert secret not in caplog.text


async def test_bare_research_command_asks_for_a_topic():
    from chatcore.plugin import CommandContext

    platform = FakePlatform()
    stores = memory_stores()
    plugin = ResearchPlugin(
        ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=None,
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=lambda *_args: None,
        bot_name="test_bot",
    )
    incoming = message("/research")
    ctx = CommandContext(
        incoming.chat, incoming.user, False, False, platform, stores, message=incoming
    )
    command = next(c for c in plugin.commands() if c.name == "research")
    asked = await command.handler(ctx, "  ")
    assert asked.awaits_reply and "What should I research?" in asked.text
    assert platform.sent == []
