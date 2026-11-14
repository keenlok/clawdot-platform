"""The use_skill catalog keeps every loadable skill selectable, however long the summaries are.

Regression for the 2026-11-14 image follow-up investigation: with 21 eligible skills the
1,000-character catalog stopped after 17 entries and the argument enum dropped the rest, so a
skill declared late (image generation) could not be loaded at all.
"""

from datetime import UTC, datetime

import pytest
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.plugin import BasePlugin
from chatcore.skill_router import SkillRouter
from chatcore.skill_turn import SkillSettings, SkillTurn
from chatcore.skills import MAX_CATALOG_SKILLS, Gate, GateState, Skill, SkillsMode, ToolGate
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolContext, ToolResult

EXAMPLES = ("one", "two", "three", "four")
ON = SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword")


class CatalogPlugin(BasePlugin):
    """``count`` filler skills with long summaries, then one late skill with its own tool."""

    name = "catalog"

    def __init__(self, count=20, *, summary="s" * 120, late_state=GateState.AVAILABLE):
        self.count = count
        self.summary = summary
        self.late_state = late_state
        self.late_calls = 0

    def tools(self):
        async def filler(ctx):
            return ToolResult("ok")

        async def late(ctx):
            self.late_calls += 1
            return ToolResult("late ran")

        empty = {"type": "object", "properties": {}}
        return [
            Tool("filler_tool", "Filler", empty, filler),
            Tool("late_tool", "Late", empty, late),
        ]

    def skills(self):
        fillers = [
            Skill(f"filler_{index:02d}", self.summary, EXAMPLES, ("filler_tool",))
            for index in range(self.count)
        ]
        return [*fillers, Skill("late_skill", "Late skill summary", EXAMPLES, ("late_tool",))]

    def tool_gates(self):
        async def late_gate(chat, is_dm):
            return Gate(self.late_state, "Late is off here.")

        return {"late_tool": ToolGate(scope=late_gate)}


def _app(plugin, stores=None, **options):
    return BotApp(
        FakePlatform(),
        stores=stores or memory_stores(),
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
        **options,
    )


async def _turn(plugin, settings=ON):
    app = _app(plugin)
    incoming = message("unrelated")
    ctx = ToolContext(incoming.chat, incoming.user, False, app.platform, app.stores)
    turn = await SkillTurn.build(
        app=app,
        ctx=ctx,
        message=incoming,
        text="unrelated",
        settings=settings,
        router=SkillRouter(
            app.skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05
        ),
        store=ConversationStore(),
        now=datetime(2026, 11, 14, tzinfo=UTC),
    )
    return app, ctx, turn


def _enum(turn):
    return turn.offered["use_skill"].parameters["properties"]["name"]["enum"]


async def test_late_skill_stays_selectable_when_summaries_exceed_the_description_budget():
    app, _ctx, turn = await _turn(CatalogPlugin())
    spec = turn.offered["use_skill"]
    assert len(spec.description) <= 1000
    assert _enum(turn) == list(turn._loadable())
    assert "late_skill" in _enum(turn)
    # The late skill is also named in the human-readable catalog, not only in the enum.
    assert "late_skill" in spec.description


async def test_short_catalog_keeps_full_summary_lines():
    app, _ctx, turn = await _turn(CatalogPlugin(count=2, summary="Filler summary"))
    spec = turn.offered["use_skill"]
    assert spec.description.splitlines() == [
        f"{name} — {app.skills.get(name).summary}" for name in _enum(turn)
    ]


async def test_names_only_catalog_when_even_short_summaries_cannot_fit():
    plugin = CatalogPlugin(count=MAX_CATALOG_SKILLS - 4)
    app, _ctx, turn = await _turn(plugin)
    spec = turn.offered["use_skill"]
    assert len(spec.description) <= 1000
    assert "late_skill" in _enum(turn)
    assert len(_enum(turn)) == len(turn._loadable())


def test_catalog_size_is_bounded_at_build_time():
    with pytest.raises(ValueError, match="skill catalog"):
        _app(CatalogPlugin(count=MAX_CATALOG_SKILLS + 1))


async def test_off_late_skill_is_listed_and_unconfigured_is_hidden():
    _app_off, _ctx, off = await _turn(CatalogPlugin(late_state=GateState.OFF))
    assert "late_skill" in _enum(off)
    _app_none, _ctx, hidden = await _turn(CatalogPlugin(late_state=GateState.UNCONFIGURED))
    assert "late_skill" not in _enum(hidden)
    assert "late_skill" not in hidden.offered["use_skill"].description


async def test_catalog_drops_a_loaded_skill_and_disappears_at_the_load_limit():
    _app_, ctx, turn = await _turn(CatalogPlugin())
    result = await turn.use_skill({"name": "late_skill"}, ctx)
    assert not result.is_error
    assert "late_skill" not in _enum(turn)
    assert "late_tool" in turn.offered
    await turn.use_skill({"name": "filler_00"}, ctx)
    assert "use_skill" not in turn.offered


async def test_late_skill_loads_and_its_tool_runs_only_from_the_next_step():
    plugin = CatalogPlugin()
    app = _app(plugin)

    def call(call_id, name, arguments=None):
        return ToolCall(call_id, name, arguments or {})

    provider = ScriptedProvider(
        [
            # Same step: the load is honoured, the not-yet-offered tool call is refused.
            Completion(
                Message.assistant(
                    "",
                    (call("c1", "use_skill", {"name": "late_skill"}), call("c2", "late_tool")),
                ),
                "tool_use",
            ),
            Completion(Message.assistant("", (call("c3", "late_tool"),)), "tool_use"),
            Completion(Message.assistant("done"), "end_turn"),
        ]
    )
    app.responder = AgentResponder(
        provider,
        system_prompt="A bot.",
        settings=AgentSettings(skills=ON),
        limits=Limits(app.stores.cache),
    )
    await app.handle(message("unrelated", mention=True))
    first, second = provider.calls[0], provider.calls[1]
    assert "late_tool" not in {spec.name for spec in first["tools"]}
    assert (
        "late_skill"
        in next(spec for spec in first["tools"] if spec.name == "use_skill").parameters[
            "properties"
        ]["name"]["enum"]
    )
    assert "late_tool" in {spec.name for spec in second["tools"]}
    assert plugin.late_calls == 1


def _responder(app, provider):
    app.responder = AgentResponder(
        provider,
        system_prompt="A bot.",
        settings=AgentSettings(skills=ON),
        limits=Limits(app.stores.cache),
    )


def _guidance(system):
    """The two trusted paragraphs, without line wrapping differences."""
    return " ".join(system.split())


async def test_trusted_guidance_does_not_infer_status_from_a_missing_tool():
    app = _app(CatalogPlugin(count=2, summary="Filler summary"))
    provider = ScriptedProvider([Completion(Message.assistant("ok"), "end_turn")])
    _responder(app, provider)
    await app.handle(message("unrelated", mention=True))
    system = _guidance(provider.calls[0]["system"])
    # Listed skills may be OFF or NOT_ALLOWED, so absence proves nothing either way.
    assert "is installed in this deployment" not in system
    assert "is not disabled or unconfigured" not in system
    assert "neither that its feature is enabled nor that it is disabled" in system
    assert "use_skill then says so" in system
    assert "load its skill and attempt it" in system
    assert "wasn't attempted" in system
    assert "is not a feature quota" in system


async def test_capability_claims_need_current_runtime_evidence_not_any_tool_result():
    app = _app(CatalogPlugin(count=2, summary="Filler summary"))
    provider = ScriptedProvider([Completion(Message.assistant("ok"), "end_turn")])
    _responder(app, provider)
    await app.handle(message("unrelated", mention=True))
    system = _guidance(provider.calls[0]["system"])
    assert "only when a tool result" not in system
    assert "only when this turn's runtime availability says so" in system
    assert "error returned by that operation's own attempt in this turn" in system
    assert (
        "Setup documentation, fetched pages or files and earlier messages are not evidence"
        in system
    )


async def test_off_skill_is_listed_refused_on_load_and_never_offered_its_tool():
    plugin = CatalogPlugin(count=2, summary="Filler summary", late_state=GateState.OFF)
    app = _app(plugin)
    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant("", (ToolCall("c1", "use_skill", {"name": "late_skill"}),)),
                "tool_use",
            ),
            Completion(Message.assistant("It is off here."), "end_turn"),
        ]
    )
    _responder(app, provider)
    await app.handle(message("unrelated", mention=True))
    first, second = provider.calls
    loader = next(spec for spec in first["tools"] if spec.name == "use_skill")
    assert "late_skill" in loader.parameters["properties"]["name"]["enum"]
    # The loader lists an OFF skill, so the guidance must not call listed skills enabled.
    assert "is not disabled or unconfigured" not in _guidance(first["system"])
    results = [item for item in second["messages"] if item.role == "tool"]
    assert [item.text for item in results] == ["Late is off here."]
    assert "late_tool" not in {spec.name for spec in second["tools"]}
    assert plugin.late_calls == 0


async def test_available_unloaded_skill_loads_under_the_same_guidance():
    plugin = CatalogPlugin(count=2, summary="Filler summary")
    app = _app(plugin)
    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant("", (ToolCall("c1", "use_skill", {"name": "late_skill"}),)),
                "tool_use",
            ),
            Completion(Message.assistant("", (ToolCall("c2", "late_tool", {}),)), "tool_use"),
            Completion(Message.assistant("done"), "end_turn"),
        ]
    )
    _responder(app, provider)
    await app.handle(message("unrelated", mention=True))
    assert "late_tool" not in {spec.name for spec in provider.calls[0]["tools"]}
    assert "late_tool" in {spec.name for spec in provider.calls[1]["tools"]}
    assert "neither that its feature is enabled" in _guidance(provider.calls[0]["system"])
    assert plugin.late_calls == 1


async def test_step_limit_final_answer_is_not_described_as_a_feature_quota():
    plugin = CatalogPlugin(count=2, summary="Filler summary")
    app = _app(plugin, general_tools=("filler_tool",))
    looping = [
        Completion(Message.assistant("", (ToolCall(f"c{index}", "filler_tool", {}),)), "tool_use")
        for index in range(2)
    ]
    provider = ScriptedProvider([*looping, Completion(Message.assistant("final"), "end_turn")])
    app.responder = AgentResponder(
        provider,
        system_prompt="A bot.",
        settings=AgentSettings(
            skills=SkillSettings(
                mode=SkillsMode.ON,
                chats=frozenset({GROUP.key}),
                router="keyword",
                tool_steps=2,
            ),
            tool_steps=2,
        ),
        limits=Limits(app.stores.cache),
    )
    await app.handle(message("one two three four", mention=True))
    final = provider.calls[-1]["system"]
    assert "The tool budget is exhausted" in final
    assert "this turn's step or time limit" in final
    assert "not a quota" in final
