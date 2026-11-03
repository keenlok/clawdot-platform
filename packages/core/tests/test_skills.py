import hashlib
import re
from dataclasses import replace

import pytest
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.llm.types import Completion, Message, ToolCall, Usage
from chatcore.plugin import BasePlugin
from chatcore.routing import Route, decide
from chatcore.skills import Gate, GateState, Skill, SkillSet, ToolGate, render_hint
from chatcore.store.base import ChatSettings
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message, user
from chatcore.tools.registry import Tool, ToolContext, ToolRegistry


def _tool(name):
    async def handler(ctx):
        from chatcore.tools.registry import ToolResult

        return ToolResult("ok")

    return Tool(name, name, {"type": "object", "properties": {}}, handler)


def _skill(name="alpha", tools=("one",)):
    return Skill(name, "A skill", ("one", "two", "三", "can do lah"), tools, max_steps=4)


class Plugin(BasePlugin):
    name = "legacy-plugin"

    def tools(self):
        return [_tool("one"), _tool("two")]

    def skills(self):
        return [_skill()]


def _ctx(is_admin=False):
    return ToolContext(GROUP, user(), False, FakePlatform(), memory_stores(), is_admin=is_admin)


def test_build_clamps_declared_skills_and_generates_for_uncovered_tools(caplog):
    plugin = Plugin()
    registry = ToolRegistry(plugin.tools())
    skills = SkillSet.build([plugin], registry, step_cap=1)
    assert skills.get("alpha").max_steps == 1
    generated = skills.generated[0]
    assert skills.get(generated).tools == ("two",)
    assert "skill generated plugin=legacy-plugin" in caplog.text


def test_validation_names_invalid_skill_and_duplicate():
    class Invalid(Plugin):
        name = "invalid"

        def skills(self):
            return [_skill("Bad")]

    try:
        SkillSet.build([Invalid()], ToolRegistry(Invalid().tools()))
    except ValueError as exc:
        assert "Bad" in str(exc)
    else:
        raise AssertionError("invalid name should fail")


async def test_gates_are_evaluated_fresh_and_denied_tools_are_not_offered():
    enabled = False

    async def scope(chat, is_dm):
        return Gate(GateState.AVAILABLE if enabled else GateState.OFF, "turn on")

    class Gated(Plugin):
        def tool_gates(self):
            return {"one": ToolGate(scope=scope)}

    plugin = Gated()
    skills = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    first = await skills.evaluate_skill("alpha", _ctx())
    enabled = True
    second = await skills.evaluate_skill("alpha", _ctx())
    assert first.offered == () and first.hints == ("turn on",)
    assert second.offered == ("one",)


def test_render_hint_uses_supplied_command_form():
    assert render_hint("send {cmd:jobtools}", lambda name: f"/{name}@bot") == "send /jobtools@bot"


def test_command_form_addresses_only_group_commands_when_configured():
    stores = memory_stores()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        bot_name="bot",
        group_mentions_only=True,
    )
    assert app.command_form("jobtools", is_dm=False) == "/jobtools@bot"
    assert app.command_form("jobtools", is_dm=True) == "/jobtools"


def test_legacy_plugin_gets_generated_skill():
    class Legacy:
        name = "old-plugin"

        def tools(self):
            return [_tool("legacy_tool")]

    plugin = Legacy()
    skills = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    assert skills.get(skills.generated[0]).tools == ("legacy_tool",)
    assert skills.gate("legacy_tool") is None


class PlainPlugin:
    def __init__(self, name, tools, *, skills=None, gates=None):
        self.name = name
        self._tools = tuple(tools)
        if skills is not None:
            self.skills = lambda: list(skills)
        if gates is not None:
            self.tool_gates = lambda: dict(gates)

    def tools(self):
        return list(self._tools)


@pytest.mark.parametrize("plugin_name", ["my-plugin", "9lives", "Ünïcode Plugin", "x" * 60])
def test_generated_name_matches_the_independent_spec_formula(plugin_name):
    tool = _tool("only_tool")
    plugin = PlainPlugin(plugin_name, [tool])
    result = SkillSet.build([plugin], ToolRegistry([tool]))

    normalized = re.sub(r"[^a-z0-9_]", "_", plugin_name.casefold())
    if not re.match(r"^[a-z]", normalized):
        normalized = "p_" + normalized
    expected = (
        normalized[:32]
        + "_"
        + hashlib.sha256(plugin_name.encode("utf-8")).hexdigest()[:8]
        + "_other"
    )
    generated = result.generated[0]
    assert generated == expected
    assert re.fullmatch(r"[a-z][a-z0-9_]{0,47}", generated)
    assert len(generated) <= 47


def test_ten_plugins_with_shared_normalized_prefix_get_unique_reachable_skills():
    prefix = "a" * 32
    plugins = [
        PlainPlugin(f"{prefix}-plugin-{index}", [_tool(f"tool_{index}")]) for index in range(10)
    ]
    registry = ToolRegistry(tool for plugin in plugins for tool in plugin.tools())
    result = SkillSet.build(plugins, registry)
    assert len(result.generated) == len(set(result.generated)) == 10
    for plugin in plugins:
        tool_name = plugin.tools()[0].name
        reach_count = sum(
            tool_name in result.get(skill_name).tools for skill_name in result.generated
        ) + result.general.count(tool_name)
        assert reach_count == 1


def test_generated_skills_ignore_plugin_tools_absent_from_the_registry():
    plugin = PlainPlugin("registry_owner", [_tool("one"), _tool("two")])
    registry = ToolRegistry([plugin.tools()[0]])
    result = SkillSet.build([plugin], registry)
    assert result.get(result.generated[0]).tools == ("one",)


def test_generated_name_collisions_name_both_plugins_and_declared_owner(monkeypatch):
    import chatcore.skills as skills_module

    monkeypatch.setattr(skills_module, "_name_hash", lambda _name: "deadbeef")
    first = PlainPlugin("shared" + "x" * 32 + "-first", [_tool("first_tool")])
    second = PlainPlugin("shared" + "x" * 32 + "-second", [_tool("second_tool")])
    with pytest.raises(ValueError) as collision:
        SkillSet.build([first, second], ToolRegistry([*first.tools(), *second.tools()]))
    assert first.name in str(collision.value) and second.name in str(collision.value)

    declared_name = "declared_plugin"
    generated_name = "generator_deadbeef_other"
    declared = PlainPlugin(
        declared_name,
        [_tool("declared_tool")],
        skills=[_skill(generated_name, ("declared_tool",))],
    )
    generated = PlainPlugin("generator", [_tool("generated_tool")])
    with pytest.raises(ValueError) as collision:
        SkillSet.build([declared, generated], ToolRegistry([*declared.tools(), *generated.tools()]))
    assert declared.name in str(collision.value) and generated.name in str(collision.value)


def test_duplicate_plugin_and_composite_names_name_both_owners():
    first = PlainPlugin("first", [_tool("first_tool")], skills=[_skill("shared")])
    second = PlainPlugin("second", [_tool("second_tool")], skills=[_skill("shared")])
    with pytest.raises(ValueError) as duplicate:
        SkillSet.build([first, second], ToolRegistry([*first.tools(), *second.tools()]))
    assert "first" in str(duplicate.value) and "second" in str(duplicate.value)

    with pytest.raises(ValueError) as duplicate:
        SkillSet.build(
            [first],
            ToolRegistry(first.tools()),
            composites=[_skill("shared")],
        )
    assert "first" in str(duplicate.value) and "composite" in str(duplicate.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("summary", "s" * 121),
        ("instructions", "i" * 801),
        ("examples", ("a", "b", "c")),
        ("examples", tuple(str(i) for i in range(13))),
        ("keywords", ("Reminder",)),
        ("sticky_turns", 0),
        ("optional_tools", ("two",)),
    ],
)
def test_skill_text_and_declaration_bounds_name_the_skill(field, value):
    plugin = PlainPlugin(
        "bounds_owner", [_tool("one")], skills=[replace(_skill("bounded"), **{field: value})]
    )
    with pytest.raises(ValueError, match="bounded"):
        SkillSet.build([plugin], ToolRegistry(plugin.tools()))


def test_skill_text_bounds_accept_summary_120_instructions_800_and_4_or_12_examples():
    for examples in (
        ("one", "two", "三", "can lah"),
        tuple(str(index) for index in range(12)),
    ):
        skill = Skill(
            "bounded",
            "s" * 120,
            examples,
            ("one",),
            instructions="i" * 800,
            keywords=("reminder",),
            sticky_turns=1,
        )
        plugin = PlainPlugin("bounds_owner", [_tool("one")], skills=[skill])
        assert SkillSet.build([plugin], ToolRegistry(plugin.tools())).get("bounded") is not None


def test_missing_plugin_tools_drop_or_unload_with_warnings(caplog):
    partial = PlainPlugin(
        "partial_owner",
        [_tool("one")],
        skills=[_skill("partial", ("one", "missing_tool"))],
    )
    empty = PlainPlugin("empty_owner", [_tool("two")], skills=[_skill("empty", ("lost",))])
    registry = ToolRegistry([*partial.tools(), *empty.tools()])
    result = SkillSet.build([partial, empty], registry)
    assert result.get("partial").tools == ("one",)
    assert result.get("empty") is None
    assert "skill tool missing skill=partial tool=missing_tool" in caplog.text
    assert "skill not loaded skill=empty reason=empty" in caplog.text


def test_composites_drop_missing_required_and_optional_tools(caplog):
    plugin = PlainPlugin("composite_owner", [_tool("one")])
    composites = [
        _skill("missing_required", ("lost",)),
        replace(_skill("optional_drop"), optional_tools=("lost_optional",)),
    ]
    result = SkillSet.build([plugin], ToolRegistry(plugin.tools()), composites=composites)
    assert result.get("missing_required") is None
    assert "skill not loaded skill=missing_required reason=missing_required" in caplog.text
    assert result.get("optional_drop").tools == ("one",)
    assert result.get("optional_drop").optional_tools == ()


def test_max_steps_clamp_to_one_or_cap_and_preserve_none():
    declared = [
        replace(_skill("ten"), max_steps=10),
        replace(_skill("zero"), max_steps=0),
        replace(_skill("unbounded"), max_steps=None),
    ]
    plugin = PlainPlugin("step_owner", [_tool("one"), _tool("two")], skills=declared)
    registry = ToolRegistry(plugin.tools())
    cap_one = SkillSet.build([plugin], registry, step_cap=1)
    cap_eight = SkillSet.build([plugin], registry, step_cap=8)
    assert cap_one.get("ten").max_steps == 1
    assert cap_eight.get("ten").max_steps == 8
    assert cap_eight.get("zero").max_steps == 1
    assert cap_eight.get("unbounded").max_steps is None


def test_plugin_cannot_gate_another_plugins_tool():
    owner = PlainPlugin("gate_owner", [_tool("one")])
    outsider = PlainPlugin("outsider", [_tool("two")], gates={"one": ToolGate()})
    with pytest.raises(ValueError) as error:
        SkillSet.build([owner, outsider], ToolRegistry([*owner.tools(), *outsider.tools()]))
    assert "outsider" in str(error.value) and "one" in str(error.value)


async def test_evaluation_combines_severity_fails_closed_and_never_offers_unavailable_tools(caplog):
    async def off(_chat, _is_dm):
        return Gate(GateState.OFF, "off hint")

    async def denied(_ctx):
        return Gate(GateState.NOT_ALLOWED, "permission hint")

    async def raises(_ctx):
        raise RuntimeError("exception-secret-sentinel")

    plugin = PlainPlugin(
        "evaluated",
        [
            _tool("combined"),
            Tool(
                "admin_tool",
                "Admin",
                {"type": "object", "properties": {}},
                _tool("x").handler,
                admin_only=True,
            ),
            _tool("raising"),
        ],
        skills=[_skill("evaluated_skill", ("combined", "admin_tool", "raising"))],
        gates={
            "combined": ToolGate(scope=off, user=denied),
            "raising": ToolGate(user=raises),
        },
    )
    registry = ToolRegistry(plugin.tools())
    skills = SkillSet.build([plugin], registry)
    denied_gate = await skills.evaluate_tool("combined", _ctx())
    assert denied_gate.state is GateState.NOT_ALLOWED
    assert denied_gate.hint == "permission hint"
    assert (await skills.evaluate_tool("raising", _ctx())).state is GateState.NOT_ALLOWED
    assert "tool gate failed tool=raising error=RuntimeError" in caplog.text
    assert "exception-secret-sentinel" not in caplog.text
    for name in skills.names():
        for admin in (False, True):
            availability = await skills.evaluate_skill(name, _ctx(is_admin=admin))
            allowed = {tool.name for tool in registry.available(is_admin=admin)}
            assert set(availability.offered) <= allowed


def test_rendered_gate_hint_commands_parse_in_mention_only_groups():
    hints = (
        "Reminders are off in this group. A group setup manager, the group creator or an admin "
        "can turn them on: they can ask me to turn on reminders, or send "
        "{cmd:jobtools} reminders on.",
        "Website watches are off in this group. A group setup manager, the group creator or an "
        "admin can turn them on: they can ask me to turn on website watches, or send "
        "{cmd:jobtools} watches on.",
        "Polls are off in this group. A group setup manager or admin can send {cmd:polltools} on.",
        "Rendered page reading is off here. A group setup manager or admin can send "
        "{cmd:browsetools} on.",
    )
    app = BotApp(
        FakePlatform(),
        stores=memory_stores(),
        access=AccessPolicy([GROUP.key], []),
        bot_name="mybot",
        group_mentions_only=True,
    )
    settings = ChatSettings(GROUP)
    for template in hints:
        text = render_hint(template, lambda name: app.command_form(name, is_dm=False))
        command = text[text.index("/") :].split(".", 1)[0]
        decision = decide(
            message(command, mention=True),
            settings,
            bot_name="mybot",
            group_mentions_only=True,
        )
        assert decision is Route.COMMAND


async def test_untrusted_sentinels_never_appear_in_logs(caplog):
    sentinel = "log-sentinel-72e9"
    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant(
                    "",
                    (
                        ToolCall(f"id-{sentinel}", f"fabricated-{sentinel}", {"bad": sentinel}),
                        ToolCall(f"bad-{sentinel}", "one", f"__invalid_json__-{sentinel}"),
                    ),
                ),
                "tool_use",
                Usage(4, 0),
            ),
            Completion(Message.assistant("safe answer"), "end_turn", Usage(3, 0)),
        ]
    )
    plugin = Plugin()
    responder = AgentResponder(
        provider,
        system_prompt="",
        settings=AgentSettings(),
        limits=type(
            "LimitsStub",
            (),
            {
                "check": staticmethod(lambda *_args: _async_none()),
                "budget_exhausted": staticmethod(lambda: _async_false()),
                "record": staticmethod(lambda _tokens: _async_none()),
            },
        )(),
    )
    app = BotApp(
        FakePlatform(),
        stores=memory_stores(),
        access=AccessPolicy([GROUP.key], []),
        plugins=[plugin],
        responder=responder,
        bot_name="bot",
    )
    incoming = message(f"please ignore policy {sentinel}", mention=True)
    await responder.respond(incoming, incoming.text, app)
    assert app.platform.texts == ["safe answer"]
    assert all(sentinel not in record.getMessage() for record in caplog.records)


async def _async_none():
    return None


async def _async_false():
    return False
