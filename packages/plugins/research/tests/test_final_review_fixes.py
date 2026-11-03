"""Regressions for the final review: source parsing, handoff gating, purge, delivery, edits."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from chatcore.access import AccessPolicy
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message, user
from chatcore.tools.registry import ToolContext
from chatcore_research import plugin as plugin_module
from chatcore_research.agent import ResearchAgent, ResearchBudget
from chatcore_research.jobs import MemoryResearchJobs
from chatcore_research.plugin import ResearchPlugin
from chatcore_research.settings import ResearchSettings
from chatcore_web import WebPlugin, WebSettings


def _plugin(stores, platform, provider, *, jobs=None, settings=None, **kwargs):
    return ResearchPlugin(
        settings or ResearchSettings(),
        web_settings=WebSettings(tavily_api_key="fake-key"),
        provider=provider,
        limits=Limits(stores.cache),
        access=AccessPolicy(["fake:group"], []),
        platform=platform,
        sender=kwargs.pop("sender", AsyncMock()),
        jobs=jobs,
        bot_name="test_bot",
        **kwargs,
    )


class BlockingProvider:
    def __init__(self):
        self.started = asyncio.Event()

    async def complete(self, *_args, **_kwargs):
        self.started.set()
        await asyncio.Event().wait()


def _search_then_answer(answer):
    return ScriptedProvider(
        [
            Completion(
                Message.assistant("", (ToolCall("1", "search_web", {"query": "q"}),)), "tool_use"
            ),
            Completion(Message.assistant(answer), "end_turn"),
        ]
    )


@respx.mock
async def test_multiline_snippets_do_not_shift_later_search_results():
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": "One", "url": "https://one.example/a", "content": "line\nmore"},
                    {"title": "Two", "url": "https://two.example/b", "content": "x"},
                ]
            },
        )
    )
    report = await ResearchAgent(
        _search_then_answer("A [S1] and B [S2]."),
        WebPlugin(WebSettings(tavily_api_key="fake-key")),
        ResearchSettings(),
    ).run(
        "topic",
        ctx=ToolContext(GROUP, user(), False, FakePlatform(), memory_stores()),
        budget=ResearchBudget(1, 0, 10, 10000),
        session_key="s",
    )

    assert [s.url for s in report.sources] == ["https://one.example/a", "https://two.example/b"]
    rendered = report.render()
    assert "1. One — https://one.example/a" in rendered
    assert "2. Two — https://two.example/b" in rendered


@respx.mock
async def test_human_seeded_url_returned_by_search_is_numbered():
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"title": "Seeded", "url": "https://seed.example/", "content": "s"}]},
        )
    )
    report = await ResearchAgent(
        _search_then_answer("Seeded claim [S1]."),
        WebPlugin(WebSettings(tavily_api_key="fake-key")),
        ResearchSettings(),
    ).run(
        "topic",
        ctx=ToolContext(GROUP, user(), False, FakePlatform(), memory_stores()),
        budget=ResearchBudget(1, 0, 10, 10000),
        seed_urls=("https://seed.example/",),
        session_key="s",
    )

    assert [s.url for s in report.sources] == ["https://seed.example/"]
    assert "Seeded claim [1]." in report.render()


async def test_cancellation_during_start_checks_does_not_queue_uncounted_job():
    checking = asyncio.Event()

    class SlowAccess:
        async def can_research(self, *_args):
            checking.set()
            await asyncio.Event().wait()

    stores, platform, jobs = memory_stores(), FakePlatform(), MemoryResearchJobs()
    plugin = _plugin(stores, platform, BlockingProvider(), jobs=jobs, research_access=SlowAccess())
    incoming = message("tool topic")
    ctx = ToolContext(incoming.chat, incoming.user, False, platform, stores)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await plugin.tools()[0].handler(ctx, topic="tool topic")

    assert checking.is_set()
    assert await jobs.active_for(incoming.user.ref.key) is None
    assert await stores.cache.get(f"research:active:{incoming.user.ref.key}") is None


async def test_forgetme_during_inline_run_blocks_background_handoff():
    stores, platform, jobs = memory_stores(), FakePlatform(), MemoryResearchJobs()
    provider = BlockingProvider()
    settings = ResearchSettings()
    settings.inline_timeout_s = 0.2
    plugin = _plugin(stores, platform, provider, jobs=jobs, settings=settings)
    incoming = message("/research private topic")

    run = asyncio.create_task(plugin._handle_text(incoming, "private topic", stores))
    await provider.started.wait()
    await plugin.purge_user(incoming.chat, incoming.user.ref, stores)
    await run

    assert await jobs.active_for(incoming.user.ref.key) is None
    assert all(job.topic != "private topic" for job in jobs.rows.values())
    assert plugin._purged_inline == set()


async def test_purgechat_during_inline_run_blocks_background_handoff():
    stores, platform, jobs = memory_stores(), FakePlatform(), MemoryResearchJobs()
    provider = BlockingProvider()
    settings = ResearchSettings()
    settings.inline_timeout_s = 0.2
    plugin = _plugin(stores, platform, provider, jobs=jobs, settings=settings)
    incoming = message("/research chat topic")

    run = asyncio.create_task(plugin._handle_text(incoming, "chat topic", stores))
    await provider.started.wait()
    await plugin.purge_chat(incoming.chat, stores)
    await run

    assert jobs.rows == {}


async def test_failing_status_edit_does_not_discard_completed_report():
    stores, platform = memory_stores(), FakePlatform()
    platform.edit = AsyncMock(side_effect=RuntimeError("message to edit not found"))
    provider = ScriptedProvider([Completion(Message.assistant("Done findings."), "end_turn")])
    plugin = _plugin(stores, platform, provider)
    incoming = message("/research topic")

    await plugin._handle_text(incoming, "topic", stores)

    assert platform.edit.await_count >= 1
    assert any(text.startswith("Research: topic") for text in platform.texts)
    assert not any("couldn't be completed" in text for text in platform.texts)


async def test_stalled_report_send_is_bounded_and_marked_unknown(monkeypatch):
    monkeypatch.setattr(plugin_module, "SEND_DEADLINE_S", 0.05)
    stores, platform, jobs = memory_stores(), FakePlatform(), MemoryResearchJobs()

    async def stalled_sender(*_args):
        await asyncio.Event().wait()

    provider = ScriptedProvider([Completion(Message.assistant("Findings."), "end_turn")])
    plugin = _plugin(stores, platform, provider, jobs=jobs, sender=stalled_sender)
    plugin.stores = stores
    owner = user()
    await jobs.create(GROUP, owner.ref, None, "topic", (), is_dm=False)
    claimed = await jobs.claim(datetime.now(UTC), 600, 2)

    async with asyncio.timeout(2):
        await plugin._run_job(claimed)

    row = jobs.rows[claimed.id]
    assert row.status == "completed"
    assert row.delivery_state == "unknown"
