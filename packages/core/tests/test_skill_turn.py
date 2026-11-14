import itertools
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin, ConversationStore
from chatcore.embed import FakeEmbedder
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.plugin import BasePlugin
from chatcore.skill_router import SkillRouter
from chatcore.skill_turn import SkillSettings, SkillTurn, effective_mode
from chatcore.skills import Gate, GateState, Skill, SkillAvailability, SkillsMode, ToolGate
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolContext, ToolResult, TurnState


class TurnPlugin(BasePlugin):
    name = "turn"

    def __init__(self):
        self.enabled = True
        self.calls = 0
        self.error_result = False

    def tools(self):
        async def hidden(ctx):
            if self.error_result:
                return ToolResult.error("simulated failure")
            return ToolResult("ok")

        return [Tool("hidden_tool", "Hidden", {"type": "object", "properties": {}}, hidden)]

    def skills(self):
        return [
            Skill(
                "feature",
                "Feature summary",
                ("trigger a", "trigger b", "trigger c", "trigger d"),
                ("hidden_tool",),
                keywords=("trigger",),
            ),
            Skill(
                "other_feature",
                "Another feature",
                ("another one", "another two", "another three", "another four"),
                ("hidden_tool",),
                keywords=("another",),
            ),
            Skill(
                "third_feature",
                "Third feature",
                ("third one", "third two", "third three", "third four"),
                ("hidden_tool",),
            ),
        ]

    def tool_gates(self):
        async def gate(chat, is_dm):
            self.calls += 1
            return Gate(
                GateState.AVAILABLE if self.enabled else GateState.OFF,
                "Feature is off: {cmd:jobtools} reminders on.",
            )

        return {"hidden_tool": ToolGate(scope=gate)}


async def _build_turn_from_state(*, state, text, now, settings=None, store=None, stores=None):
    plugin = TurnPlugin()
    stores = stores or memory_stores()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    conversation = store or ConversationStore()
    old = message("prior request")
    await stores.repo.log_message(old)
    if state is not None:
        await conversation.add(
            old.chat,
            old.thread_id,
            "assistant",
            {"skill_state": {"requester": old.user.ref.key, "skills": state}},
            old.message_id,
        )
    incoming = message(text)
    await stores.repo.log_message(incoming)
    ctx = ToolContext(incoming.chat, incoming.user, False, app.platform, stores)
    turn = await SkillTurn.build(
        app=app,
        ctx=ctx,
        message=incoming,
        text=text,
        settings=settings
        or SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword"),
        router=SkillRouter(
            app.skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05
        ),
        store=conversation,
        now=now,
    )
    return app, stores, conversation, ctx, turn, incoming


def _state_items(*names, left, fresh_at):
    return [{"name": name, "left": left, "fresh_at": fresh_at.isoformat()} for name in names]


def test_effective_mode_empty_allowlist_and_wildcard():
    assert effective_mode(SkillSettings(mode=SkillsMode.ON), GROUP) is SkillsMode.OFF
    assert (
        effective_mode(SkillSettings(mode=SkillsMode.ON, chats=frozenset({"*"})), GROUP)
        is SkillsMode.ON
    )
    assert effective_mode(SkillSettings(mode=SkillsMode.SHADOW), GROUP) is SkillsMode.SHADOW


async def test_skill_turn_offers_only_gated_skill_tools_and_use_skill_is_state_neutral():
    plugin = TurnPlugin()
    stores = memory_stores()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    incoming = message("unrelated")
    turn_state = TurnState(
        read_untrusted=True, untrusted_tools={"read_attachment"}, seen_urls={"https://x.invalid"}
    )
    ctx = ToolContext(
        incoming.chat,
        incoming.user,
        incoming.is_dm,
        app.platform,
        stores,
        turn=turn_state,
    )
    settings = SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword")
    router = SkillRouter(
        app.skills, FakeEmbedder(), mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05
    )
    skill_turn = await SkillTurn.build(
        app=app,
        ctx=ctx,
        message=incoming,
        text="unrelated",
        settings=settings,
        router=router,
        store=ConversationStore(),
        now=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert skill_turn.active == []
    assert "hidden_tool" not in skill_turn.offered
    spec = skill_turn.offered["use_skill"]
    assert spec.parameters["properties"]["name"]["enum"] == [
        "conversation",
        "feature",
        "other_feature",
        "third_feature",
        "explanation",
    ]
    turn_state.read_untrusted = True
    turn_state.untrusted_tools = {"fetch_web_page"}
    turn_state.seen_urls = {"https://x.invalid"}
    before = asdict(deepcopy(turn_state))
    result = await skill_turn.use_skill({"name": "feature"}, ctx)
    assert result.persist_content is False
    assert result.untrusted is False
    assert "hidden_tool" in skill_turn.offered
    assert before == asdict(deepcopy(turn_state))
    off_state = deepcopy(turn_state)
    plugin.enabled = False
    off_result = await skill_turn.use_skill({"name": "other_feature"}, ctx)
    assert off_result.is_error
    assert before == asdict(deepcopy(turn_state))
    error_result = await skill_turn.use_skill({"name": "unknown"}, ctx)
    assert error_result.is_error and error_result.persist_content is False
    assert before == asdict(deepcopy(turn_state))
    assert asdict(off_state) == before


async def test_shadow_snapshot_evaluates_a_shared_gate_once():
    plugin = TurnPlugin()
    stores = memory_stores()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    incoming = message("trigger this")
    ctx = ToolContext(incoming.chat, incoming.user, False, app.platform, stores)
    skill_turn = await SkillTurn.build(
        app=app,
        ctx=ctx,
        message=incoming,
        text="trigger this",
        settings=SkillSettings(mode=SkillsMode.SHADOW, router="keyword"),
        router=SkillRouter(
            app.skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05
        ),
        store=ConversationStore(),
        now=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert plugin.calls == 1
    assert skill_turn.eligible_snapshot["hidden_tool"] is GateState.AVAILABLE
    assert skill_turn.metric_fields(["hidden_tool"])["initial_cover"] == "1/1"


async def test_continuation_record_expires_inherited_skills_without_mutating_state():
    plugin = TurnPlugin()
    stores = memory_stores()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    incoming = message("unrelated")
    ctx = ToolContext(incoming.chat, incoming.user, False, app.platform, stores)
    settings = SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword")
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
        now=datetime(2025, 1, 1, tzinfo=UTC),
    )
    turn.active = ["feature"]
    turn._state["feature"] = (1, datetime(2024, 12, 31, tzinfo=UTC))
    before = dict(turn._state)
    first = turn.continuation_record(datetime(2025, 1, 1, tzinfo=UTC))
    second = turn.continuation_record(datetime(2025, 1, 1, tzinfo=UTC))
    assert first == second
    assert first["skills"] == []
    assert turn._state == before


async def test_use_skill_enum_evaluates_catalog_and_excludes_unconfigured(monkeypatch):
    plugin = TurnPlugin()
    stores = memory_stores()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    for index in range(12):
        name = f"catalog_{index}"
        skill = Skill(
            name,
            "s" * 120,
            ("one", "two", "three", "four"),
            ("hidden_tool",),
        )
        app.skills._skills[name] = (skill, "turn")
    incoming = message("unrelated")
    ctx = ToolContext(incoming.chat, incoming.user, False, app.platform, stores)
    original = app.skills.evaluate_skill
    calls = {}

    async def evaluate(name, tool_ctx):
        calls[name] = calls.get(name, 0) + 1
        if name == "other_feature":
            return SkillAvailability(GateState.UNCONFIGURED)
        return await original(name, tool_ctx)

    monkeypatch.setattr(app.skills, "evaluate_skill", evaluate)
    turn = await SkillTurn.build(
        app=app,
        ctx=ctx,
        message=incoming,
        text="unrelated",
        settings=SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword"),
        router=SkillRouter(
            app.skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05
        ),
        store=ConversationStore(),
        now=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert calls["other_feature"] == 1
    spec = turn.offered["use_skill"]
    names = spec.parameters["properties"]["name"]["enum"]
    assert names[:3] == ["conversation", "feature", "third_feature"]
    assert "other_feature" not in names
    assert len(spec.description) <= 1000
    # Twelve 120-character summaries exceed the budget: summaries are shortened evenly, and
    # every loadable skill stays both described and selectable.
    assert names[-1] == "catalog_11"
    assert [line.split(" — ", 1)[0] for line in spec.description.splitlines()] == names
    assert all("…" in line for line in spec.description.splitlines() if "catalog_" in line)


async def test_use_skill_arguments_are_validated_without_echoing_names():
    plugin = TurnPlugin()
    stores = memory_stores()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    incoming = message("unrelated")
    ctx = ToolContext(incoming.chat, incoming.user, False, app.platform, stores)
    turn = await SkillTurn.build(
        app=app,
        ctx=ctx,
        message=incoming,
        text="unrelated",
        settings=SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword"),
        router=SkillRouter(
            app.skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05
        ),
        store=ConversationStore(),
        now=datetime(2025, 1, 1, tzinfo=UTC),
    )
    for arguments in ({}, {"name": 1}, {"name": "feature", "extra": "sentinel"}):
        result = await turn.use_skill(arguments, ctx)
        assert result.content == "use_skill needs one argument: name."
        assert result.is_error and result.persist_content is False and not result.untrusted


async def test_use_skill_not_allowed_returns_rendered_hint_without_offering_tools(monkeypatch):
    app, _stores, _conversation, ctx, turn, _incoming = await _build_turn_from_state(
        state=None,
        text="unrelated",
        now=datetime(2025, 1, 1, tzinfo=UTC),
    )
    app.group_mentions_only = True

    async def not_allowed(_name, _ctx):
        return SkillAvailability(
            GateState.NOT_ALLOWED,
            hints=("Ask an admin: {cmd:jobtools} reminders on.",),
        )

    monkeypatch.setattr(app.skills, "evaluate_skill", not_allowed)
    before = turn.offered_snapshot()
    result = await turn.use_skill({"name": "feature"}, ctx)
    assert result.is_error
    assert result.persist_content is False
    assert "/jobtools@testbot reminders on" in result.content
    assert turn.offered_snapshot() == before


async def test_use_skill_rechecks_shared_gate_for_each_call_and_build(monkeypatch):
    plugin = TurnPlugin()
    stores = memory_stores()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    incoming = message("unrelated")
    ctx = ToolContext(incoming.chat, incoming.user, False, app.platform, stores)
    router = SkillRouter(
        app.skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05
    )
    settings = SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword")
    turn = await SkillTurn.build(
        app=app,
        ctx=ctx,
        message=incoming,
        text="unrelated",
        settings=settings,
        router=router,
        store=ConversationStore(),
        now=datetime(2025, 1, 1, tzinfo=UTC),
    )
    before = plugin.calls
    loaded = await turn.use_skill({"name": "feature"}, ctx)
    assert not loaded.is_error
    assert plugin.calls == before + 1
    plugin.enabled = False
    off = await turn.use_skill({"name": "other_feature"}, ctx)
    assert off.is_error and "/jobtools reminders on" in off.content
    assert plugin.calls == before + 2
    second = message("unrelated again")
    second_ctx = ToolContext(second.chat, second.user, False, app.platform, stores)
    await SkillTurn.build(
        app=app,
        ctx=second_ctx,
        message=second,
        text="unrelated",
        settings=settings,
        router=router,
        store=ConversationStore(),
        now=datetime(2025, 1, 1, 0, 1, tzinfo=UTC),
    )
    assert plugin.calls > before + 2


async def test_offered_set_stays_subset_of_available_tools_for_catalog_combinations():
    class MatrixPlugin(BasePlugin):
        name = "matrix"

        def tools(self):
            async def okay(_ctx):
                return ToolResult("ok")

            return [
                Tool("public_tool", "public", {"type": "object", "properties": {}}, okay),
                Tool(
                    "admin_tool",
                    "admin",
                    {"type": "object", "properties": {}},
                    okay,
                    admin_only=True,
                ),
                Tool("off_tool", "off", {"type": "object", "properties": {}}, okay),
                Tool("denied_tool", "denied", {"type": "object", "properties": {}}, okay),
            ]

        def skills(self):
            return [
                Skill(
                    name,
                    name,
                    (f"{name} one", f"{name} two", f"{name} three", f"{name} four"),
                    (tool,),
                    keywords=(name,),
                )
                for name, tool in (
                    ("public_skill", "public_tool"),
                    ("admin_skill", "admin_tool"),
                    ("off_skill", "off_tool"),
                    ("denied_skill", "denied_tool"),
                )
            ]

        def tool_gates(self):
            async def off(_chat, _is_dm):
                return Gate(GateState.OFF, "feature is off")

            async def denied(_chat, _is_dm):
                return Gate(GateState.NOT_ALLOWED, "not allowed")

            return {"off_tool": ToolGate(scope=off), "denied_tool": ToolGate(scope=denied)}

    names = ("public_skill", "admin_skill", "off_skill", "denied_skill", "ghost_skill")
    for is_admin in (False, True):
        for size in range(4):
            for selected in itertools.combinations(names, size):
                plugin = MatrixPlugin()
                stores = memory_stores()
                app = BotApp(
                    FakePlatform(),
                    stores=stores,
                    access=AccessPolicy([GROUP.key], []),
                    plugins=[ConversationPlugin(), plugin],
                    bot_name="testbot",
                )
                ghost = Skill(
                    "ghost_skill",
                    "Ghost",
                    ("ghost one", "ghost two", "ghost three", "ghost four"),
                    ("missing_tool",),
                    keywords=("ghost_skill",),
                )
                app.skills._skills[ghost.name] = (ghost, "matrix")
                text = " ".join(selected) or "unrelated"
                incoming = message(text)
                ctx = ToolContext(
                    incoming.chat,
                    incoming.user,
                    False,
                    app.platform,
                    stores,
                    is_admin=is_admin,
                )
                turn = await SkillTurn.build(
                    app=app,
                    ctx=ctx,
                    message=incoming,
                    text=text,
                    settings=SkillSettings(
                        mode=SkillsMode.ON,
                        chats=frozenset({GROUP.key}),
                        router="keyword",
                        max_active=3,
                        max_loads=2,
                    ),
                    router=SkillRouter(
                        app.skills,
                        None,
                        mode="keyword",
                        timeout_ms=100,
                        threshold=0.5,
                        margin=0.05,
                    ),
                    store=ConversationStore(),
                    now=datetime(2025, 1, 1, tzinfo=UTC),
                )
                available = {tool.name for tool in app.tools.available(is_admin=is_admin)}
                assert set(turn.offered) - {"use_skill"} <= available
                enum = turn.offered["use_skill"].parameters["properties"]["name"]["enum"]
                loadable = next(
                    (
                        name
                        for name in enum
                        if turn.all_availability[name].state is GateState.AVAILABLE
                    ),
                    None,
                )
                if loadable:
                    result = await turn.use_skill({"name": loadable}, ctx)
                    assert not result.is_error
                    assert set(turn.offered) - {"use_skill"} <= available


async def test_fresh_skill_beats_inherited_at_cap_and_crowded_skill_decrements():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    old = _state_items("other_feature", left=2, fresh_at=now - timedelta(minutes=1))
    _app, _stores, _store, _ctx, turn, _incoming = await _build_turn_from_state(
        state=old,
        text="trigger the feature",
        now=now,
        settings=SkillSettings(
            mode=SkillsMode.ON,
            chats=frozenset({GROUP.key}),
            router="keyword",
            max_active=1,
        ),
    )
    assert turn.active == ["feature"]
    assert turn._inherited_crowded == {"other_feature"}
    record = turn.continuation_record(now)
    values = {item["name"]: item["left"] for item in record["skills"]}
    assert values == {"feature": 2, "other_feature": 1}


async def test_crowded_fresh_skill_refreshes_its_continuation_counter():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    _app, _stores, _store, _ctx, turn, _incoming = await _build_turn_from_state(
        state=None,
        text="trigger and another",
        now=now,
        settings=SkillSettings(
            mode=SkillsMode.ON,
            chats=frozenset({GROUP.key}),
            router="keyword",
            max_active=1,
        ),
    )
    assert turn.active == ["feature"]
    assert turn._inherited_crowded == {"other_feature"}
    records = {item["name"]: item for item in turn.continuation_record(now)["skills"]}
    assert records["other_feature"]["left"] == 2
    assert records["other_feature"]["fresh_at"] == now.isoformat()


async def test_executed_crowded_inherited_skill_is_refreshed():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    _app, _stores, _store, _ctx, turn, _incoming = await _build_turn_from_state(
        state=_state_items("feature", "other_feature", left=1, fresh_at=now - timedelta(minutes=1)),
        text="unrelated",
        now=now,
        settings=SkillSettings(
            mode=SkillsMode.ON,
            chats=frozenset({GROUP.key}),
            router="keyword",
            max_active=1,
        ),
    )
    assert turn._inherited_crowded == {"other_feature"}
    turn.note_executed("hidden_tool")
    records = {item["name"]: item for item in turn.continuation_record(now)["skills"]}
    assert records["other_feature"]["left"] == 2
    assert records["other_feature"]["fresh_at"] == now.isoformat()


async def test_loading_shorter_skill_does_not_shrink_initial_step_cap():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    _app, _stores, _store, ctx, turn, _incoming = await _build_turn_from_state(
        state=None,
        text="unrelated",
        now=now,
    )
    assert turn.step_cap == 8
    short = replace(turn._app.skills.get("feature"), max_steps=2)
    turn._app.skills._skills["feature"] = (short, "turn")
    result = await turn.use_skill({"name": "feature"}, ctx)
    assert not result.is_error
    assert turn.step_cap == 8


async def test_capping_skill_without_offered_tools_does_not_limit_turn():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    _app, _stores, _store, _ctx, turn, _incoming = await _build_turn_from_state(
        state=None,
        text="trigger feature",
        now=now,
    )
    skill = replace(turn._app.skills.get("feature"), max_steps=2)
    turn._app.skills._skills["feature"] = (skill, "turn")
    turn.availability["feature"] = SkillAvailability(GateState.OFF)
    turn.step_cap = turn._current_step_cap()
    assert turn.step_cap == 8


async def test_general_call_lifts_capped_skill_to_global_step_cap():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    _app, _stores, _store, _ctx, turn, _incoming = await _build_turn_from_state(
        state=None,
        text="trigger feature",
        now=now,
    )
    skill = replace(turn._app.skills.get("feature"), max_steps=2)
    turn._app.skills._skills["feature"] = (skill, "turn")
    turn.availability["feature"] = SkillAvailability(GateState.AVAILABLE, ("hidden_tool",))
    turn.step_cap = turn._current_step_cap()
    assert turn.step_cap == 2
    turn.note_call("search_web")
    assert turn.step_cap == 8


async def test_capped_skill_own_tool_keeps_skill_cap():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    _app, _stores, _store, _ctx, turn, _incoming = await _build_turn_from_state(
        state=None,
        text="trigger feature",
        now=now,
    )
    skill = replace(turn._app.skills.get("feature"), max_steps=2)
    turn._app.skills._skills["feature"] = (skill, "turn")
    turn.availability["feature"] = SkillAvailability(GateState.AVAILABLE, ("hidden_tool",))
    turn.step_cap = turn._current_step_cap()
    turn.note_call("hidden_tool")
    assert turn.step_cap == 2


async def test_tool_from_uncapped_active_skill_lifts_capped_skill_limit():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    _app, _stores, _store, _ctx, turn, _incoming = await _build_turn_from_state(
        state=None,
        text="trigger feature and another",
        now=now,
    )
    turn.active = ["feature", "other_feature"]
    short = replace(turn._app.skills.get("feature"), max_steps=2)
    turn._app.skills._skills["feature"] = (short, "turn")
    turn.availability["feature"] = SkillAvailability(GateState.AVAILABLE, ("hidden_tool",))
    turn.availability["other_feature"] = SkillAvailability(GateState.AVAILABLE, ("other_tool",))
    turn.step_cap = turn._current_step_cap()
    turn.note_call("other_tool")
    assert turn.step_cap == 8


async def test_routed_unavailable_skill_without_hint_does_not_take_active_slot():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    plugin = TurnPlugin()
    plugin.enabled = False
    plugin.skills = lambda: [replace(TurnPlugin().skills()[0], keywords=("trigger",), max_steps=2)]
    plugin.tool_gates = lambda: {
        "hidden_tool": ToolGate(scope=lambda _chat, _is_dm: _off_without_hint())
    }

    async def _off_without_hint():
        return Gate(GateState.OFF)

    stores = memory_stores()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    incoming = message("trigger feature")
    ctx = ToolContext(incoming.chat, incoming.user, False, app.platform, stores)
    turn = await SkillTurn.build(
        app=app,
        ctx=ctx,
        message=incoming,
        text="trigger feature",
        settings=SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword"),
        router=SkillRouter(
            app.skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05
        ),
        store=ConversationStore(),
        now=now,
    )
    assert turn.active == []
    assert turn.step_cap == 8


async def test_gated_skill_with_hint_stays_active_without_setting_cap():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    plugin = TurnPlugin()
    plugin.enabled = False
    skills = plugin.skills
    plugin.skills = lambda: [replace(skill, max_steps=2) for skill in skills()]
    stores = memory_stores()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(), plugin],
        bot_name="testbot",
    )
    incoming = message("trigger feature")
    ctx = ToolContext(incoming.chat, incoming.user, False, app.platform, stores)
    turn = await SkillTurn.build(
        app=app,
        ctx=ctx,
        message=incoming,
        text="trigger feature",
        settings=SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword"),
        router=SkillRouter(
            app.skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05
        ),
        store=ConversationStore(),
        now=now,
    )
    assert turn.active == ["feature"]
    assert any("Feature is off" in hint for _skill, hint in turn.hints)
    assert turn.step_cap == 8


async def test_three_inherited_skills_expire_and_new_keyword_becomes_active():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    state = _state_items("feature", "other_feature", "third_feature", left=2, fresh_at=now)
    store = None
    stores = None
    last_turn = None
    for index in range(2):
        last_turn = await _build_turn_from_state(
            state=state,
            text="unrelated",
            now=now + timedelta(seconds=index),
            store=store,
            stores=stores,
        )
        _app, stores, store, _ctx, turn, incoming = last_turn
        assert set(turn.active) == {"feature", "other_feature", "third_feature"}
        state = turn.continuation_record(now + timedelta(seconds=index))["skills"]
        await store.add(
            incoming.chat,
            incoming.thread_id,
            "assistant",
            {"skill_state": {"requester": incoming.user.ref.key, "skills": state}},
            incoming.message_id,
        )
    assert state == []
    _app, _stores, _store, _ctx, fresh, _incoming = await _build_turn_from_state(
        state=state,
        text="trigger feature",
        now=now + timedelta(seconds=3),
        store=store,
        stores=stores,
    )
    assert fresh.active == ["feature"]


@pytest.mark.parametrize("refresh", ["fresh", "load", "executed"])
async def test_continuation_refreshes_only_on_match_load_or_successful_execution(refresh):
    now = datetime(2025, 1, 1, tzinfo=UTC)
    old_fresh_at = now - timedelta(minutes=1)
    state = _state_items("feature", left=1, fresh_at=old_fresh_at)
    text = "trigger feature" if refresh == "fresh" else "unrelated"
    app, stores, _store, ctx, turn, _incoming = await _build_turn_from_state(
        state=state if refresh != "load" else [],
        text=text,
        now=now,
    )
    if refresh == "load":
        await turn.use_skill({"name": "feature"}, ctx)
    elif refresh == "executed":
        turn.note_executed("hidden_tool")
    record = turn.continuation_record(now)
    entries = {item["name"]: item for item in record["skills"]}
    assert entries["feature"]["left"] == 2
    assert entries["feature"]["fresh_at"] == now.isoformat()


@pytest.mark.parametrize("call_kind", ["rejected", "error"])
async def test_rejected_or_erroring_agent_tool_call_does_not_refresh_continuation(call_kind):
    now = datetime(2025, 1, 2, tzinfo=UTC)
    old_fresh_at = now - timedelta(minutes=1)
    plugin = TurnPlugin()
    plugin.error_result = call_kind == "error"
    plugin.enabled = call_kind == "error"
    stores = memory_stores()
    store = ConversationStore()
    conversation = ConversationPlugin(store)
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[conversation, plugin],
        bot_name="testbot",
    )
    prior = message("prior", sender=message("prior").user)
    await stores.repo.log_message(prior)
    state = {
        "requester": prior.user.ref.key,
        "skills": _state_items("feature", left=2, fresh_at=old_fresh_at),
    }
    await store.add(
        prior.chat, prior.thread_id, "assistant", {"skill_state": state}, prior.message_id
    )
    incoming = message("unrelated followup", sender=prior.user)
    calls = [ToolCall(f"{call_kind}-call", "hidden_tool", {})]
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("", tuple(calls)), "tool_use"),
            Completion(Message.assistant("done"), "end_turn"),
        ]
    )
    settings = SkillSettings(
        mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword", sticky_turns=2
    )
    responder = AgentResponder(
        provider,
        system_prompt="legacy",
        settings=AgentSettings(skills=settings),
        limits=Limits(stores.cache),
        clock=lambda: now,
    )
    await responder.respond(incoming, incoming.text, app)
    rows = await store.recent(incoming.chat, incoming.thread_id)
    final = next(
        row for row in reversed(rows) if row.role == "assistant" and "skill_state" in row.content
    )
    saved = final.content["skill_state"]["skills"]
    feature = next(item for item in saved if item["name"] == "feature")
    assert feature["left"] == 1
    assert feature["fresh_at"] == old_fresh_at.isoformat()


async def test_inherited_skill_older_than_thirty_minutes_is_dropped():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    _app, _stores, _store, _ctx, turn, _incoming = await _build_turn_from_state(
        state=_state_items("feature", left=2, fresh_at=now - timedelta(minutes=31)),
        text="unrelated",
        now=now,
    )
    assert turn.active == []
    assert turn._state == {}
