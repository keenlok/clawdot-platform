from datetime import UTC, datetime

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.plugin import BasePlugin
from chatcore.skill_turn import SkillSettings, SkillTurn
from chatcore.skills import Skill, SkillsMode
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolResult


class WebResearchPlugin(BasePlugin):
    name = "web_research"

    def __init__(self):
        self.calls = 0

    def tools(self):
        async def called(_ctx):
            self.calls += 1
            return ToolResult("result")

        return [
            Tool("search_web", "Search", {"type": "object", "properties": {}}, called),
            Tool("fetch_web_page", "Fetch", {"type": "object", "properties": {}}, called),
            Tool("rendered_tool", "Rendered", {"type": "object", "properties": {}}, called),
        ]

    def skills(self):
        return [
            Skill(
                "rendered_pages",
                "Rendered pages",
                ("render pages", "render a page", "rendered site", "rendering pages"),
                ("rendered_tool",),
                max_steps=3,
                keywords=("render",),
            )
        ]


def _completion(text="", calls=(), reason="end_turn"):
    return Completion(Message.assistant(text, tuple(calls)), reason)


async def _run_web_turn(tool_call_count, monkeypatch):
    plugin = WebResearchPlugin()
    tools = ["search_web" if index % 2 == 0 else "fetch_web_page" for index in range(9)]
    responses = [
        *[
            _completion("", [ToolCall(f"web-{index}", tools[index], {})], "tool_use")
            for index in range(tool_call_count)
        ],
        _completion("Research answer."),
    ]
    provider = ScriptedProvider(responses)
    observed = {}
    original_note_call = SkillTurn.note_call

    def capture_note_call(turn, tool_name):
        if "turn" not in observed:
            observed["turn"] = turn
            observed["cap_before_first_call"] = turn.step_cap
        original_note_call(turn, tool_name)

    monkeypatch.setattr(SkillTurn, "note_call", capture_note_call)
    stores = memory_stores()
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
            ),
            tool_steps=8,
        ),
        limits=Limits(stores.cache),
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )
    app.responder = responder
    await responder.respond(message("render this research"), "render this research", app)
    return provider, plugin, platform, observed


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
    assert provider.calls[-1]["tool_choice"] == "none"
    assert observed["turn"].active == ["rendered_pages"]
    assert "rendered_tool" in observed["turn"].offered
    assert observed["cap_before_first_call"] == 3
