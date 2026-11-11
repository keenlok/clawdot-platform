from __future__ import annotations

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.limits import Limits, LimitsSettings
from chatcore.llm.types import Completion, Message, ToolCall, Usage
from chatcore.plugin import BasePlugin
from chatcore.skill_turn import SkillSettings, SkillTurn
from chatcore.skills import Gate, GateState, Skill, SkillAvailability, SkillsMode, ToolGate
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolResult


class WorkPlugin(BasePlugin):
    name = "output_budget_test"

    def tools(self):
        async def work(ctx):
            return ToolResult("worked")

        return [Tool("work", "Work", {"type": "object", "properties": {}}, work)]


class OutputSkillPlugin(WorkPlugin):
    def __init__(self, *, restricted=False):
        self.restricted = restricted

    def skills(self):
        return [
            Skill(
                "visual_summary",
                "Cards",
                ("card", "summary", "卡片", "lah card"),
                ("work",),
                keywords=("card",),
                max_steps=6,
                max_output_tokens=9000,
            ),
            Skill(
                "loaded_summary",
                "Loaded cards",
                ("load", "card", "加载", "lah load"),
                ("work",),
                max_steps=6,
                max_output_tokens=10000,
            ),
        ]

    def tool_gates(self):
        if not self.restricted:
            return {}

        async def deny(_chat, _is_dm):
            return Gate(GateState.NOT_ALLOWED, "Unavailable")

        return {"work": ToolGate(scope=deny)}


def _completion(text="", *, calls=(), reason="end_turn", tokens=10):
    return Completion(Message.assistant(text, tuple(calls)), reason, Usage(tokens, 0))


async def _run_agent(provider, *, plugin=None, settings=None, text="budget request"):
    stores = memory_stores()
    limits = Limits(stores.cache, LimitsSettings(DAILY_TOKEN_BUDGET=100_000))
    stores.cache = limits.cache
    responder = AgentResponder(
        provider, system_prompt="", settings=settings or AgentSettings(), limits=limits
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin or WorkPlugin()],
        responder=responder,
        bot_name="bot",
    )
    incoming = message(text, mention=True)
    store = responder._store(app)

    async def record_usage(*_args):
        return None

    store.record_usage = record_usage
    await responder.respond(incoming, incoming.text, app)
    return app


def test_output_budget_uses_offered_active_skill_and_ceiling():
    skill = Skill(
        "visual", "summary", ("a", "b", "c", "d"), ("tool",), max_steps=6, max_output_tokens=12288
    )

    class Skills:
        def get(self, name):
            return skill if name == "visual" else None

    turn = SkillTurn(
        SkillsMode.ON,
        ["visual"],
        [],
        {},
        [],
        [],
        8,
        availability={"visual": SkillAvailability(GateState.AVAILABLE, ("tool",))},
    )
    turn._app = type("App", (), {"skills": Skills()})()
    assert turn.output_tokens(2048, 12288) == 12288
    assert turn.output_tokens(2048, 4096) == 4096
    turn.availability["visual"] = SkillAvailability(GateState.AVAILABLE, ())
    assert turn.output_tokens(2048, 12288) == 2048


def test_validate_rejects_zero_output_budget():
    from chatcore.skills import SkillSet

    skill = Skill("visual", "summary", ("a", "b", "c", "d"), ("tool",), max_output_tokens=0)
    try:
        SkillSet._validate(skill, "test", composite=False)
    except ValueError as exc:
        assert "max_output_tokens" in str(exc)
    else:
        raise AssertionError("invalid skill accepted")


def _skill_agent_settings(*, mode=SkillsMode.ON, ceiling=12000):
    return AgentSettings(
        max_tokens=2048,
        max_tokens_ceiling=ceiling,
        skills=SkillSettings(mode=mode, chats=frozenset({"*"}), router="keyword"),
    )


async def test_agent_output_budget_defaults_without_skill_set():
    provider = ScriptedProvider([_completion("answer")])
    await _run_agent(provider, settings=AgentSettings(max_tokens=1777))
    assert provider.calls[0]["max_tokens"] == 1777


async def test_agent_output_budget_uses_active_offered_skill_and_ceiling():
    provider = ScriptedProvider([_completion("answer")])
    await _run_agent(
        provider,
        plugin=OutputSkillPlugin(),
        settings=_skill_agent_settings(ceiling=8000),
        text="make a card",
    )
    assert provider.calls[0]["max_tokens"] == 8000


async def test_agent_output_budget_uses_loaded_skill_on_following_step():
    provider = ScriptedProvider(
        [
            _completion(
                calls=[ToolCall("load", "use_skill", {"name": "loaded_summary"})],
                reason="tool_use",
            ),
            _completion("answer"),
        ]
    )
    await _run_agent(
        provider,
        plugin=OutputSkillPlugin(),
        settings=_skill_agent_settings(),
    )
    assert [call["max_tokens"] for call in provider.calls] == [2048, 10000]


async def test_agent_output_budget_ignores_inactive_and_not_offered_skills():
    inactive = ScriptedProvider([_completion("answer")])
    await _run_agent(
        inactive,
        plugin=OutputSkillPlugin(),
        settings=_skill_agent_settings(),
        text="ordinary request",
    )
    assert inactive.calls[0]["max_tokens"] == 2048

    restricted = ScriptedProvider([_completion("answer")])
    await _run_agent(
        restricted,
        plugin=OutputSkillPlugin(restricted=True),
        settings=_skill_agent_settings(),
        text="make a card",
    )
    assert restricted.calls[0]["max_tokens"] == 2048


async def test_agent_output_budget_is_ignored_when_skills_mode_is_off():
    provider = ScriptedProvider([_completion("answer")])
    await _run_agent(
        provider,
        plugin=OutputSkillPlugin(),
        settings=_skill_agent_settings(mode=SkillsMode.OFF),
        text="make a card",
    )
    assert provider.calls[0]["max_tokens"] == 2048
