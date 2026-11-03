import asyncio

import chatcore.agent as agent_module
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.llm.base import ProviderError
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.plugin import BasePlugin
from chatcore.testing import GROUP, FakePlatform, memory_stores, message
from chatcore.tools.registry import Tool, ToolResult


def completion(text="answer", calls=(), reason="end_turn"):
    return Completion(Message.assistant(text, tuple(calls)), reason)


class DelayedProvider:
    name = "deadline-test"

    def __init__(self, responses, *, delays=(), fail_at=None):
        self.responses = list(responses)
        self.delays = list(delays)
        self.fail_at = fail_at
        self.calls = []

    async def complete(
        self,
        messages,
        *,
        system,
        tools=(),
        tool_choice="auto",
        max_tokens=2048,
        session_key=None,
    ):
        index = len(self.calls)
        self.calls.append(
            {
                "messages": list(messages),
                "system": system,
                "tools": list(tools),
                "tool_choice": tool_choice,
            }
        )
        if index < len(self.delays):
            await asyncio.sleep(self.delays[index])
        if index == self.fail_at:
            raise ProviderError("fake provider failure")
        return self.responses[index]


def build(
    provider,
    *,
    settings=None,
    platform=None,
    tool_handler=None,
    stores=None,
):
    stores = stores or memory_stores()
    platform = platform or FakePlatform()

    class TestPlugin(BasePlugin):
        name = "deadline_test"

        def tools(self):
            if tool_handler is None:
                return []
            return [
                Tool(
                    "work",
                    "Do test work",
                    {"type": "object", "properties": {"label": {"type": "string"}}},
                    tool_handler,
                )
            ]

    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=settings or AgentSettings(timeout_s=1, final_answer_reserve_s=0.4),
        limits=type(
            "AllowLimits",
            (),
            {"check": _allow, "record": _record, "budget_exhausted": _no_budget},
        )(),
    )
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        bot_name="bot",
        responder=responder,
        plugins=[TestPlugin()],
    )
    return app, platform


async def _allow(*_args):
    return None


async def _record(*_args):
    return None


async def _no_budget(*_args):
    return False


async def test_timed_out_tool_records_errors_for_remaining_calls_then_answers():
    called = []

    async def work(_ctx, *, label):
        called.append(label)
        if label == "slow":
            await asyncio.sleep(0.8)
        return ToolResult("finished")

    provider = DelayedProvider(
        [
            completion(
                "",
                [
                    ToolCall("one", "work", {"label": "slow"}),
                    ToolCall("two", "work", {"label": "skip"}),
                ],
                "tool_use",
            ),
            completion("Final from reserved time."),
        ]
    )
    app, platform = build(provider, tool_handler=work)
    await app.handle(message("do work", mention=True))
    assert called == ["slow"]
    assert platform.texts == ["Final from reserved time."]
    final_call = provider.calls[-1]
    assert final_call["tool_choice"] == "none"
    results = [item for item in final_call["messages"] if item.role.value == "tool"]
    assert [item.text for item in results] == [
        "The tool ran out of time.",
        "Not run: the turn ran out of time.",
    ]
    assert all(item.is_error for item in results)


async def test_slow_nonfinal_provider_call_is_discarded_for_final_answer():
    provider = DelayedProvider(
        [
            completion("discarded", [ToolCall("unused", "work", {})], "tool_use"),
            completion("Final answer."),
        ],
        delays=(0.8,),
    )
    app, platform = build(provider, tool_handler=lambda *_args, **_kwargs: ToolResult("ran"))
    await app.handle(message("question", mention=True))
    assert platform.texts == ["Final answer."]
    assert len(provider.calls) == 2
    assert provider.calls[-1]["tool_choice"] == "none"


async def test_reserve_forces_current_request_to_final_without_running_its_tools():
    ran = []

    async def work(_ctx, **_args):
        ran.append("ran")
        await asyncio.sleep(0.1)
        return ToolResult("done")

    provider = DelayedProvider(
        [
            completion("", [ToolCall("one", "work", {})], "tool_use"),
            completion("Final answer.", [ToolCall("also-ignored", "work", {})], "tool_use"),
        ],
        delays=(1.25,),
    )
    app, platform = build(
        provider,
        settings=AgentSettings(timeout_s=2, final_answer_reserve_s=0.7),
        tool_handler=work,
    )
    await app.handle(message("question", mention=True))
    assert ran == ["ran"]
    assert platform.texts == ["Final answer."]
    assert provider.calls[-1]["tool_choice"] == "none"


async def test_hanging_final_step_keeps_existing_timeout_answer():
    provider = DelayedProvider([completion("never returned")], delays=(1,))
    app, platform = build(
        provider, settings=AgentSettings(timeout_s=0.2, final_answer_reserve_s=0.05, tool_steps=0)
    )
    await app.handle(message("question", mention=True))
    assert platform.texts == ["I ran out of time while working on that. Please try again."]


async def test_typing_refresh_stops_after_answer_and_provider_error(monkeypatch):
    monkeypatch.setattr(agent_module, "_TYPING_REFRESH_INTERVAL_S", 0.01)
    provider = DelayedProvider([completion("slow answer")], delays=(0.07,))
    app, platform = build(
        provider, settings=AgentSettings(timeout_s=0.5, final_answer_reserve_s=0.1, tool_steps=0)
    )
    await app.handle(message("question", mention=True))
    count_after_answer = platform.typing_calls
    assert count_after_answer >= 2
    await asyncio.sleep(0.03)
    assert platform.typing_calls == count_after_answer

    failing = DelayedProvider([], fail_at=0)
    failed_app, failed_platform = build(
        failing, settings=AgentSettings(timeout_s=0.5, final_answer_reserve_s=0.1, tool_steps=0)
    )
    await failed_app.handle(message("question", mention=True))
    count_after_error = failed_platform.typing_calls
    await asyncio.sleep(0.03)
    assert failed_platform.typing_calls == count_after_error


async def test_bot_app_uses_configured_turn_lock_ttl():
    stores = memory_stores()
    observed = []
    original_lock = stores.cache.lock

    def lock(key, *, ttl_s):
        observed.append((key, ttl_s))
        return original_lock(key, ttl_s=ttl_s)

    stores.cache.lock = lock
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        bot_name="bot",
        turn_lock_ttl_s=17,
    )
    await app.handle(message("hello", mention=True))
    assert (f"turn:{GROUP.key}", 17) in observed


async def test_typing_stops_once_answer_is_sent_while_summary_runs(monkeypatch):
    from chatcore.conversation import CompactResult, ConversationStore
    from chatcore.llm.types import Usage

    monkeypatch.setattr(agent_module, "_TYPING_REFRESH_INTERVAL_S", 0.01)

    async def slow_compact(*_args, **_kwargs):
        await asyncio.sleep(0.08)
        return CompactResult(Usage())

    monkeypatch.setattr(ConversationStore, "compact", slow_compact)

    class SnapshotPlatform(FakePlatform):
        at_send = None

        async def send(self, *args, **kwargs):
            self.at_send = self.typing_calls
            return await super().send(*args, **kwargs)

    provider = DelayedProvider([completion("slow answer")], delays=(0.05,))
    app, platform = build(
        provider,
        settings=AgentSettings(timeout_s=0.5, final_answer_reserve_s=0.1, tool_steps=0),
        platform=SnapshotPlatform(),
    )
    await app.handle(message("question", mention=True))
    assert platform.texts == ["slow answer"]
    assert platform.at_send >= 2
    assert platform.typing_calls == platform.at_send
