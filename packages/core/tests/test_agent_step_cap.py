import logging
from datetime import UTC, datetime

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.limits import Limits, LimitsSettings
from chatcore.llm.base import ProviderError
from chatcore.llm.types import Completion, Message, ToolCall, Usage
from chatcore.plugin import BasePlugin
from chatcore.skill_turn import SkillSettings, SkillTurn
from chatcore.skills import Gate, GateState, Skill, SkillsMode, ToolGate
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolResult


async def _pin(_ctx):
    return True


class WebResearchPlugin(BasePlugin):
    name = "web_research"

    def __init__(self):
        self.calls = 0
        self.deny_final_gate = False

    def tools(self):
        async def called(_ctx):
            self.calls += 1
            return ToolResult("result")

        return [
            Tool("search_web", "Search", {"type": "object", "properties": {}}, called),
            Tool("fetch_web_page", "Fetch", {"type": "object", "properties": {}}, called),
            Tool("rendered_tool", "Rendered", {"type": "object", "properties": {}}, called),
            Tool("rendered_tool2", "Rendered 2", {"type": "object", "properties": {}}, called),
        ]

    def tool_gates(self):
        async def gate(_chat, _is_dm):
            return Gate(
                GateState.OFF if self.deny_final_gate and self.calls >= 8 else GateState.AVAILABLE,
                "Final delivery is currently unavailable.",
            )

        return {"rendered_tool": ToolGate(scope=gate)}

    def skills(self, final_tool=False, second_final_tool=False):
        skills = [
            Skill(
                "rendered_pages",
                "Rendered pages",
                ("render pages", "render a page", "rendered site", "rendering pages"),
                ("rendered_tool",),
                max_steps=3,
                keywords=("render",),
                final_tools=("rendered_tool",) if final_tool else (),
                pin=_pin if second_final_tool else None,
            )
        ]
        if second_final_tool:
            skills.append(
                Skill(
                    "research_pages",
                    "Research pages",
                    ("research page", "research pages", "research site", "research result"),
                    ("rendered_tool2",),
                    max_steps=3,
                    keywords=("research",),
                    final_tools=("rendered_tool2",),
                    pin=_pin,
                )
            )
        return skills


def _completion(text="", calls=(), reason="end_turn"):
    return Completion(Message.assistant(text, tuple(calls)), reason)


async def _run_web_turn(
    tool_call_count,
    monkeypatch,
    *,
    final_tool=False,
    already_called_final=False,
    loop_guard=False,
    timeout_reserved=False,
    provider_error_reserved=False,
    no_call_reserved=False,
    deadline=False,
    budget=False,
    multiple_final_tools=False,
    gate_denial=False,
    truncated_reserved=False,
    token_limit=None,
):
    plugin = WebResearchPlugin()
    plugin.deny_final_gate = gate_denial
    if final_tool or multiple_final_tools:
        plugin.skills = lambda: WebResearchPlugin.skills(
            plugin,
            final_tool=final_tool or multiple_final_tools,
            second_final_tool=multiple_final_tools,
        )
    tools = ["search_web" if index % 2 == 0 else "fetch_web_page" for index in range(9)]
    if loop_guard:
        tools = ["search_web"] * len(tools)
    if already_called_final:
        tools[0] = "rendered_tool"
    calls_to_run = 2 if loop_guard else tool_call_count
    responses = [
        *[
            _completion("", [ToolCall(f"web-{index}", tools[index], {})], "tool_use")
            for index in range(calls_to_run)
        ],
        *(
            [
                _completion("The tool was not called.")
                if no_call_reserved
                else _completion(
                    "",
                    [
                        ToolCall("reserved", "rendered_tool", {}),
                        *(
                            [ToolCall("reserved2", "rendered_tool2", {})]
                            if multiple_final_tools
                            else []
                        ),
                    ],
                    "max_tokens" if truncated_reserved else "tool_use",
                )
            ]
            if (final_tool and not (already_called_final or deadline or budget))
            or multiple_final_tools
            else []
        ),
        _completion("Research answer."),
    ]
    if token_limit is not None:
        responses[:calls_to_run] = [
            Completion(item.message, item.stop_reason, Usage(100, 0))
            for item in responses[:calls_to_run]
        ]
    provider = ScriptedProvider(responses)
    if timeout_reserved:
        provider = TimeoutReservedProvider(responses)
    if provider_error_reserved:
        provider = ProviderErrorReservedProvider(responses)
    if deadline:
        provider = TimeoutSecondProvider(responses)
    stores = memory_stores()
    limits = Limits(
        stores.cache,
        LimitsSettings(DAILY_TOKEN_BUDGET=50 if budget else 100_000),
    )
    if budget:
        responses[:] = [
            Completion(responses[0].message, "tool_use", Usage(100, 0)),
            _completion("Research answer."),
        ]
    observed = {}
    original_note_call = SkillTurn.note_call

    def capture_note_call(turn, tool_name):
        if "turn" not in observed:
            observed["turn"] = turn
            observed["cap_before_first_call"] = turn.step_cap
        original_note_call(turn, tool_name)

    monkeypatch.setattr(SkillTurn, "note_call", capture_note_call)
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
        general_tools=("search_web", "fetch_web_page"),
    )
    responder = AgentResponder(
        provider,
        system_prompt="legacy",
        settings=AgentSettings(
            skills=SkillSettings(
                mode=SkillsMode.ON,
                chats=frozenset({GROUP.key}),
                router="keyword",
                tool_steps=8,
            ),
            tool_steps=8,
            timeout_s=0.5 if deadline else 60,
            final_answer_reserve_s=0.25 if deadline else 0.01,
            # These calls repeat empty arguments on purpose; this file tests the step cap.
            max_identical_calls=1 if loop_guard else 10,
            turn_input_token_limit=token_limit,
        ),
        limits=limits,
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )
    app.responder = responder
    await responder.respond(message("render this research"), "render this research", app)
    return provider, plugin, platform, observed


class TimeoutReservedProvider(ScriptedProvider):
    async def complete(self, messages, **kwargs):
        if len(self.calls) == 8:
            self.calls.append({"tool_choice": kwargs["tool_choice"], "tools": kwargs["tools"]})
            self.responses.pop(0)
            raise TimeoutError
        return await super().complete(messages, **kwargs)


class ProviderErrorReservedProvider(ScriptedProvider):
    async def complete(self, messages, **kwargs):
        if len(self.calls) == 8:
            self.calls.append({"tool_choice": kwargs["tool_choice"], "tools": kwargs["tools"]})
            self.responses.pop(0)
            raise ProviderError("fake provider failure")
        return await super().complete(messages, **kwargs)


class TimeoutSecondProvider(ScriptedProvider):
    async def complete(self, messages, **kwargs):
        if len(self.calls) == 1:
            self.calls.append({"tool_choice": kwargs["tool_choice"], "tools": kwargs["tools"]})
            raise TimeoutError
        return await super().complete(messages, **kwargs)


async def test_routed_skill_cap_does_not_limit_general_web_calls(monkeypatch):
    provider, plugin, platform, observed = await _run_web_turn(5, monkeypatch)

    assert plugin.calls == 5
    assert platform.texts == ["Research answer."]
    assert provider.calls[-1]["tool_choice"] == "auto"
    assert observed["turn"].active == ["rendered_pages"]
    assert "rendered_tool" in observed["turn"].offered
    assert observed["cap_before_first_call"] == 3


async def test_general_web_calls_stop_at_global_step_cap(monkeypatch):
    provider, plugin, platform, observed = await _run_web_turn(8, monkeypatch)

    assert plugin.calls == 8
    assert platform.texts == ["Research answer."]
    assert len(provider.calls) == 9
    assert provider.calls[8]["tool_choice"] == "none"
    assert provider.calls[-1]["tool_choice"] == "none"
    assert observed["turn"].active == ["rendered_pages"]
    assert "rendered_tool" in observed["turn"].offered
    assert observed["cap_before_first_call"] == 3


async def test_step_cap_reserves_only_the_undelivered_final_tool(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    provider, plugin, platform, observed = await _run_web_turn(8, monkeypatch, final_tool=True)

    assert plugin.calls == 9
    assert platform.texts == ["Research answer."]
    assert len(provider.calls) == 10
    assert [spec.name for spec in provider.calls[8]["tools"]] == ["rendered_tool"]
    assert provider.calls[8]["tool_choice"] == "auto"
    assert provider.calls[9]["tool_choice"] == "none"
    assert "final_tool=rendered_tool" in caplog.text


async def test_called_final_tool_is_not_reserved_again(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    provider, plugin, _platform, _observed = await _run_web_turn(
        8, monkeypatch, final_tool=True, already_called_final=True
    )

    assert plugin.calls == 8
    assert len(provider.calls) == 9
    assert provider.calls[-1]["tool_choice"] == "none"
    assert "final_tool=-" in caplog.text


async def test_deadline_forced_turn_skips_reservation_and_answers(monkeypatch):
    provider, _plugin, platform, _observed = await _run_web_turn(
        1, monkeypatch, final_tool=True, deadline=True
    )

    assert len(provider.calls) == 3
    assert provider.calls[1]["tool_choice"] == "auto"
    assert provider.calls[-1]["tool_choice"] == "none"
    assert platform.texts == ["Research answer."]


async def test_budget_forced_turn_skips_reservation(monkeypatch):
    provider, _plugin, platform, _observed = await _run_web_turn(
        1, monkeypatch, final_tool=True, budget=True
    )

    assert len(provider.calls) == 2
    assert provider.calls[-1]["tool_choice"] == "none"
    assert platform.texts == ["Research answer."]


async def test_loop_guard_reservation_runs_its_handler(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    provider, plugin, platform, _observed = await _run_web_turn(
        2, monkeypatch, final_tool=True, loop_guard=True
    )

    assert len(provider.calls) == 4
    assert [spec.name for spec in provider.calls[2]["tools"]] == ["rendered_tool"]
    assert provider.calls[2]["tool_choice"] == "auto"
    assert plugin.calls == 2
    assert platform.texts == ["Research answer."]
    assert "final_tool=rendered_tool" in caplog.text


async def test_reserved_request_timeout_is_followed_by_no_tools_final(monkeypatch):
    provider, _plugin, platform, _observed = await _run_web_turn(
        8, monkeypatch, final_tool=True, timeout_reserved=True
    )

    assert len(provider.calls) == 10
    assert provider.calls[-1]["tools"] == []
    assert provider.calls[-1]["tool_choice"] == "none"
    assert platform.texts == ["Research answer."]


async def test_reserved_request_provider_error_is_followed_by_final(monkeypatch):
    provider, _plugin, platform, _observed = await _run_web_turn(
        8, monkeypatch, final_tool=True, provider_error_reserved=True
    )

    assert len(provider.calls) == 10
    assert provider.calls[-1]["tool_choice"] == "none"
    assert provider.calls[-1]["tools"] == []
    assert platform.texts == ["Research answer."]


async def test_truncated_reserved_call_is_dropped_and_final_follows(monkeypatch):
    provider, plugin, platform, _observed = await _run_web_turn(
        8, monkeypatch, final_tool=True, truncated_reserved=True
    )

    # No truncation-recovery retry with the reserved tools; the reserved handler never runs.
    assert len(provider.calls) == 10
    assert plugin.calls == 8
    assert provider.calls[9]["tool_choice"] == "none"
    assert provider.calls[9]["tools"] == []
    assert platform.texts == ["Research answer."]


async def test_token_limit_reached_with_step_cap_skips_reservation(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    provider, plugin, platform, _observed = await _run_web_turn(
        8, monkeypatch, final_tool=True, token_limit=750
    )

    # No reserved request: every request after the cap is a no-tools final answer.
    assert plugin.calls == 8
    assert all(call["tool_choice"] == "none" for call in provider.calls[8:])
    assert "final_tool=-" in caplog.text
    assert platform.texts


async def test_reserved_request_without_tool_call_is_followed_by_final(monkeypatch):
    provider, _plugin, platform, _observed = await _run_web_turn(
        8, monkeypatch, final_tool=True, no_call_reserved=True
    )

    assert len(provider.calls) == 10
    assert provider.calls[8]["tool_choice"] == "auto"
    assert provider.calls[9]["tool_choice"] == "none"
    assert provider.calls[9]["tools"] == []
    assert platform.texts == ["Research answer."]


async def test_denied_reserved_tool_returns_gate_error_then_answers(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    provider, plugin, platform, _observed = await _run_web_turn(
        8, monkeypatch, final_tool=True, gate_denial=True
    )

    assert plugin.calls == 8
    assert "Final delivery is currently unavailable." in provider.calls[-1]["messages"][-1].text
    assert provider.calls[-1]["tool_choice"] == "none"
    assert platform.texts == ["Research answer."]
    assert "final_tool=-" in caplog.text


async def test_two_skills_offer_final_tools_but_only_one_call_runs(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="chatcore.agent")
    provider, plugin, platform, _observed = await _run_web_turn(
        8, monkeypatch, multiple_final_tools=True
    )

    assert [spec.name for spec in provider.calls[8]["tools"]] == [
        "rendered_tool",
        "rendered_tool2",
    ]
    assert plugin.calls == 9
    results = [item.text for item in provider.calls[-1]["messages"] if item.role.value == "tool"]
    assert "Not run: only one final tool call is allowed." in results
    assert platform.texts == ["Research answer."]
    assert "final_tool=rendered_tool" in caplog.text
