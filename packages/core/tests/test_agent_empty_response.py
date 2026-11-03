"""Ordinary empty provider responses must not silently abandon an unfinished turn."""

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.limits import Limits, LimitsSettings
from chatcore.llm.types import Completion, Message, ToolCall, Usage
from chatcore.pending import PendingAction
from chatcore.plugin import BasePlugin
from chatcore.skill_turn import SkillSettings
from chatcore.skills import Skill, SkillsMode
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolResult


class SourcePlugin(BasePlugin):
    name = "source_test"

    def __init__(self):
        self.reads = 0
        self.previews = 0
        self.commits = 0

    def tools(self):
        async def read(_ctx):
            self.reads += 1
            return ToolResult("Private itinerary body", untrusted=True, persist_content=False)

        async def attach(ctx):
            self.previews += 1
            return ToolResult(
                "Attach source?",
                pending_action=PendingAction.create(
                    chat=ctx.chat,
                    requester=ctx.user.ref,
                    plugin=self.name,
                    kind="attach",
                    payload={"trip_id": "existing-trip"},
                    summary="Attach source to existing trip?",
                    thread_id=ctx.thread_id,
                ),
            )

        return [
            Tool("read_source", "Read source", {"type": "object", "properties": {}}, read),
            Tool(
                "attach_source",
                "Attach source",
                {"type": "object", "properties": {}},
                attach,
                writes=True,
            ),
        ]

    def skills(self):
        return [
            Skill(
                "source",
                "Read source",
                ("read source", "read document", "open file", "view source"),
                ("read_source",),
                keywords=("read",),
            ),
            Skill(
                "itinerary",
                "Attach source to a trip",
                ("attach trip", "save trip", "set itinerary", "trip document"),
                ("attach_source",),
            ),
        ]

    def action_handlers(self):
        return {"attach": self}

    async def commit(self, _action, _ctx):
        self.commits += 1
        return "Attached."


def response(text="", calls=(), reason="end_turn"):
    return Completion(Message.assistant(text, tuple(calls)), reason, Usage(10, 5))


def read_and_load():
    return [
        response(calls=[ToolCall("read", "read_source", {})], reason="tool_use"),
        response(calls=[ToolCall("load", "use_skill", {"name": "itinerary"})], reason="tool_use"),
    ]


async def run(script, *, budget=100_000, timeout_s=150, reserve_s=None, provider=None):
    stores = memory_stores()
    limits = Limits(stores.cache, LimitsSettings(DAILY_TOKEN_BUDGET=budget))
    plugin, platform = SourcePlugin(), FakePlatform()
    provider = provider or ScriptedProvider(script)
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="bot",
    )
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(
            timeout_s=timeout_s,
            final_answer_reserve_s=(
                reserve_s if reserve_s is not None else 0.01 if timeout_s < 5 else 25
            ),
            skills=SkillSettings(
                mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword"
            ),
        ),
        limits=limits,
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )
    app.responder = responder
    incoming = message("read source", mention=True)
    await responder.respond(incoming, incoming.text, app)
    return app, plugin, provider, limits


@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
async def test_plain_empty_after_skill_load_continues_to_confirmation(stop_reason, caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    app, plugin, provider, limits = await run(
        [
            *read_and_load(),
            response(reason=stop_reason),
            response(calls=[ToolCall("attach", "attach_source", {})], reason="tool_use"),
        ]
    )
    assert plugin.reads == plugin.previews == 1
    assert plugin.commits == 0
    assert len(provider.calls) == 4
    assert provider.calls[-1]["tool_choice"] == "auto"
    assert provider.calls[-1]["messages"] == provider.calls[-2]["messages"]
    assert "attach_source" in {tool.name for tool in provider.calls[-1]["tools"]}
    assert app.platform.sent[-1].action_id
    assert "Sorry" not in app.platform.sent[-1].text
    assert len(app.stores.repo._pending) == 1
    assert await limits.budget_used() == 60
    rows = await app.responder._store(app).recent(GROUP, None)
    assert [r.content["tool_call_id"] for r in rows if r.role == "tool"] == [
        "read",
        "load",
        "attach",
    ]
    assert "Private itinerary body" not in str(rows)
    assert "empty provider response" in caplog.text
    assert f"reason={stop_reason}" in caplog.text
    assert "Private itinerary body" not in caplog.text


async def test_repeated_plain_empty_is_bounded_and_sends_nonempty_fallback():
    app, plugin, provider, limits = await run(
        [*read_and_load(), response(), response(), response(), response()]
    )
    assert plugin.reads == 1
    assert plugin.previews == plugin.commits == 0
    assert len(provider.calls) == 6
    assert [c["tool_choice"] for c in provider.calls] == [
        "auto",
        "auto",
        "auto",
        "auto",
        "none",
        "auto",
    ]
    assert provider.calls[-1]["tools"] == []
    assert app.platform.texts[0].startswith("Sorry, I couldn't finish an answer")
    assert await limits.budget_used() == 90


@pytest.mark.parametrize("constraints", [{"budget": 45}, {"timeout_s": 2}])
async def test_plain_empty_does_not_retry_when_budget_or_time_is_insufficient(constraints):
    app, plugin, provider, limits = await run([*read_and_load(), response()], **constraints)
    assert plugin.reads == 1
    assert plugin.previews == plugin.commits == 0
    assert len(provider.calls) == 3
    assert app.platform.texts[0].startswith("Sorry, I couldn't finish an answer")
    assert await limits.budget_used() == 45


async def test_only_one_tool_capable_empty_continuation_is_allowed_per_turn():
    app, plugin, provider, limits = await run(
        [
            *read_and_load(),
            response(),
            response(calls=[ToolCall("read-again", "read_source", {})], reason="tool_use"),
            response(),
            response("The source was read; attachment needs clarification."),
        ]
    )
    assert plugin.reads == 2
    assert plugin.previews == plugin.commits == 0
    assert [c["tool_choice"] for c in provider.calls] == [
        "auto",
        "auto",
        "auto",
        "auto",
        "auto",
        "none",
    ]
    assert app.platform.texts == ["The source was read; attachment needs clarification."]
    assert await limits.budget_used() == 90


async def test_empty_continuation_timeout_preserves_final_answer_reserve():
    provider = ScriptedProvider(
        [
            *read_and_load(),
            response(),
            response(calls=[ToolCall("not-returned", "attach_source", {})], reason="tool_use"),
            response("The source was read, but no attachment has been saved."),
        ]
    )
    original = provider.complete

    async def delayed_continuation(*args, **kwargs):
        result = await original(*args, **kwargs)
        if len(provider.calls) == 4:
            await asyncio.sleep(1)
        return result

    provider.complete = delayed_continuation
    app, plugin, provider, limits = await run([], provider=provider, timeout_s=6, reserve_s=5.5)
    assert plugin.reads == 1
    assert plugin.previews == plugin.commits == 0
    assert len(provider.calls) == 5
    assert provider.calls[-1]["tool_choice"] == "none"
    assert app.platform.texts == ["The source was read, but no attachment has been saved."]
    assert await limits.budget_used() == 60
