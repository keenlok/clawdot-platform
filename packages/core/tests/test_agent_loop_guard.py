"""Per-turn tool-step cap defaults and the loop guard (identical calls, error streaks, tokens)."""

import logging
from datetime import UTC, datetime

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings, _call_key
from chatcore.app import BotApp
from chatcore.conversation import SUMMARY_SYSTEM_PROMPT, ConversationPlugin
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message, ToolCall, Usage
from chatcore.plugin import BasePlugin
from chatcore.skill_turn import SkillSettings
from chatcore.skills import Skill, SkillSet, SkillsMode
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolRegistry, ToolResult

_SCHEMA = {"type": "object", "properties": {"sheet": {"type": "string"}}}


class SheetPlugin(BasePlugin):
    name = "sheets"

    def __init__(self):
        self.calls: list[str] = []

    def tools(self):
        async def read_sheet(_ctx, sheet: str = ""):
            self.calls.append(sheet)
            return ToolResult(f"rows of {sheet}")

        async def broken(_ctx, sheet: str = ""):
            self.calls.append(sheet)
            return ToolResult.error("sheet not found")

        return [
            Tool("read_sheet", "Read a sheet", _SCHEMA, read_sheet),
            Tool("broken_sheet", "Always fails", _SCHEMA, broken),
        ]

    def skills(self):
        return [
            Skill(
                "sheets",
                "Read sheets",
                ("read the sheet", "read my sheets", "sheet rows", "spreadsheet rows"),
                ("read_sheet", "broken_sheet"),
                keywords=("sheet",),
            )
        ]


def _call(index, name="read_sheet", sheet=None, usage=None):
    arguments = {"sheet": f"s{index}" if sheet is None else sheet}
    return Completion(
        Message.assistant("", (ToolCall(f"c{index}", name, arguments),)),
        "tool_use",
        usage or Usage(),
    )


def _answer(text="Final answer.", usage=None):
    return Completion(Message.assistant(text), "end_turn", usage or Usage())


async def _run(responses, *, settings=None, skills_on=False):
    plugin = SheetPlugin()
    provider = ScriptedProvider(list(responses))
    stores = memory_stores()
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
        general_tools=("read_sheet", "broken_sheet"),
    )
    base = settings or AgentSettings()
    if skills_on:
        base = AgentSettings(
            tool_steps=base.tool_steps,
            max_identical_calls=base.max_identical_calls,
            max_consecutive_tool_errors=base.max_consecutive_tool_errors,
            turn_input_token_limit=base.turn_input_token_limit,
            skills=SkillSettings(
                mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword"
            ),
        )
    responder = AgentResponder(
        provider,
        system_prompt="system",
        settings=base,
        limits=Limits(stores.cache),
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )
    app.responder = responder
    await responder.respond(message("read the sheet"), "read the sheet", app)
    return provider, plugin, platform


def _metrics(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("turn metrics")]
    assert lines
    return lines[-1]


def _agent_calls(provider):
    """The agent loop's requests, without the conversation summary that may follow."""
    return [call for call in provider.calls if call["system"] != SUMMARY_SYSTEM_PROMPT]


def _forced_final(provider):
    return [
        index for index, call in enumerate(_agent_calls(provider)) if call["tool_choice"] == "none"
    ]


def test_default_step_cap_is_twenty():
    assert AgentSettings().tool_steps == 20
    assert SkillSettings().tool_steps == 20


def test_skill_max_steps_are_clamped_to_the_default_cap():
    class Big(BasePlugin):
        name = "big"

        def tools(self):
            async def run(_ctx):
                return ToolResult("ok")

            return [Tool("big_tool", "Big", {"type": "object", "properties": {}}, run)]

        def skills(self):
            return [
                Skill(
                    "big",
                    "Big skill",
                    ("run big", "big job", "run the big job", "big task"),
                    ("big_tool",),
                    max_steps=30,
                )
            ]

    plugin = Big()
    skills = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    assert skills.get("big").max_steps == 20


async def test_twenty_distinct_calls_run_then_final_answer(caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    provider, plugin, platform = await _run([*[_call(i) for i in range(20)], _answer()])

    assert len(plugin.calls) == 20
    assert platform.texts == ["Final answer."]
    assert _forced_final(provider) == [20]
    assert "final_forced=steps" in _metrics(caplog)


async def test_twenty_distinct_calls_run_with_skills_on(caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    provider, plugin, platform = await _run(
        [*[_call(i) for i in range(20)], _answer()], skills_on=True
    )

    assert len(plugin.calls) == 20
    assert platform.texts == ["Final answer."]
    assert _forced_final(provider) == [20]
    assert "final_forced=steps" in _metrics(caplog)


async def test_third_identical_call_is_refused_and_forces_final_answer(caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    provider, plugin, platform = await _run(
        [
            _call(0, sheet="secret-arg-value"),
            _call(1, sheet="secret-arg-value"),
            _call(2, sheet="secret-arg-value"),
            _answer(),
        ]
    )

    assert plugin.calls == ["secret-arg-value", "secret-arg-value"]
    assert platform.texts == ["Final answer."]
    assert _forced_final(provider) == [3]
    refused = _agent_calls(provider)[3]["messages"][-1]
    assert "already called with these arguments" in str(refused)
    metrics = _metrics(caplog)
    assert "final_forced=loop" in metrics
    assert metrics.endswith("repeats_refused=1")
    guard = [r.getMessage() for r in caplog.records if "loop guard" in r.getMessage()]
    assert len(guard) == 1
    assert "reason=loop tool=read_sheet" in guard[0]
    assert all("secret-arg-value" not in r.getMessage() for r in caplog.records)


async def test_identical_calls_in_one_response_count_the_same_way(caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    calls = tuple(ToolCall(f"p{i}", "read_sheet", {"sheet": "same"}) for i in range(4))
    provider, plugin, platform = await _run(
        [Completion(Message.assistant("", calls), "tool_use"), _answer()]
    )

    assert plugin.calls == ["same", "same"]
    assert platform.texts == ["Final answer."]
    assert _forced_final(provider) == [1]
    assert _metrics(caplog).endswith("repeats_refused=2")


async def test_two_identical_calls_are_allowed():
    provider, plugin, platform = await _run(
        [_call(0, sheet="a"), _call(1, sheet="a"), _call(2, sheet="b"), _answer()]
    )

    assert plugin.calls == ["a", "a", "b"]
    assert _forced_final(provider) == []
    assert platform.texts == ["Final answer."]


async def test_max_identical_calls_one_refuses_the_second():
    provider, plugin, _platform = await _run(
        [_call(0, sheet="a"), _call(1, sheet="a"), _answer()],
        settings=AgentSettings(max_identical_calls=1),
    )

    assert plugin.calls == ["a"]
    assert _forced_final(provider) == [2]


def test_call_key_ignores_argument_order_and_whitespace():
    first = ToolCall("1", "read_sheet", {"a": 1, "b": 2})
    second = ToolCall("2", "read_sheet", {"b": 2, "a": 1})
    assert _call_key(first) == _call_key(second)
    assert _call_key(ToolCall("3", "read_sheet", {"a": 1})) != _call_key(
        ToolCall("4", "read_sheet", {"a": 2})
    )
    assert _call_key(ToolCall("5", "other", {"a": 1})) != _call_key(
        ToolCall("6", "read_sheet", {"a": 1})
    )


async def test_consecutive_errors_force_final_answer(caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    provider, plugin, platform = await _run(
        [*[_call(i, name="broken_sheet") for i in range(4)], _answer()]
    )

    assert len(plugin.calls) == 4
    assert platform.texts == ["Final answer."]
    assert _forced_final(provider) == [4]
    assert "final_forced=errors" in _metrics(caplog)


async def test_a_success_resets_the_error_streak(caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    responses = [
        *[_call(i, name="broken_sheet") for i in range(3)],
        _call(3),
        *[_call(i, name="broken_sheet") for i in range(4, 7)],
        _answer(),
    ]
    provider, plugin, platform = await _run(responses)

    assert len(plugin.calls) == 7
    assert _forced_final(provider) == []
    assert "final_forced=-" in _metrics(caplog)


async def test_turn_input_token_limit_forces_final_answer(caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    responses = [
        _call(0, usage=Usage(600, 10)),
        _call(1, usage=Usage(600, 10)),
        _answer(),
    ]
    provider, plugin, _platform = await _run(
        responses, settings=AgentSettings(turn_input_token_limit=1000)
    )

    assert plugin.calls == ["s0", "s1"]
    assert _forced_final(provider) == [2]
    assert "final_forced=tokens" in _metrics(caplog)


async def test_no_token_limit_by_default(caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    responses = [_call(i, usage=Usage(100_000, 10)) for i in range(3)] + [_answer()]
    provider, plugin, _platform = await _run(responses)

    assert len(plugin.calls) == 3
    assert _forced_final(provider) == []


async def test_empty_final_answer_after_loop_uses_fallback():
    provider, plugin, platform = await _run(
        [
            _call(0, sheet="a"),
            _call(1, sheet="a"),
            _call(2, sheet="a"),
            _answer(""),
            _answer(""),
            _answer(""),
            _answer(""),
        ]
    )

    assert plugin.calls == ["a", "a"]
    assert platform.texts and platform.texts[-1].startswith("Sorry, I couldn't finish an answer")


async def test_step_limit_errors_in_one_response_keep_the_steps_label(caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    calls = tuple(ToolCall(f"p{i}", "read_sheet", {"sheet": f"s{i}"}) for i in range(6))
    provider, plugin, platform = await _run(
        [Completion(Message.assistant("", calls), "tool_use"), _answer()],
        settings=AgentSettings(tool_steps=2),
    )

    assert plugin.calls == ["s0", "s1"]
    assert platform.texts == ["Final answer."]
    assert _forced_final(provider) == [1]
    assert "final_forced=steps" in _metrics(caplog)
    assert not [r for r in caplog.records if "loop guard" in r.getMessage()]


async def test_unknown_tools_are_not_counted_as_identical_calls(caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    responses = [
        Completion(
            Message.assistant("", (ToolCall(f"u{i}", "made_up_tool", {"x": 1}),)), "tool_use"
        )
        for i in range(3)
    ]
    provider, plugin, _platform = await _run([*responses, _answer()])

    assert plugin.calls == []
    assert _metrics(caplog).endswith("repeats_refused=0")
    assert "final_forced=loop" not in _metrics(caplog)
    assert all(
        "made_up_tool" not in r.getMessage() for r in caplog.records if "guard" in r.getMessage()
    )


async def test_empty_response_retry_does_not_offer_tools_past_the_token_limit():
    responses = [
        _call(0, usage=Usage(500, 10)),
        _answer("", usage=Usage(1200, 0)),
        _answer(),
    ]
    provider, plugin, platform = await _run(
        responses, settings=AgentSettings(turn_input_token_limit=1000)
    )

    assert plugin.calls == ["s0"]
    assert platform.texts == ["Final answer."]
    assert [call["tool_choice"] for call in _agent_calls(provider)] == ["auto", "auto", "none"]
