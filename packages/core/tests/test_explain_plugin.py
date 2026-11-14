"""Explanations retrieve evidence, respect visibility and never run demonstrated actions."""

import json
from dataclasses import replace

import pytest
from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.plugin import BasePlugin
from chatcore.skills import Gate, GateState, ToolGate
from chatcore.testing import GROUP, FakePlatform, memory_stores, message, user
from chatcore.tools.registry import Tool, ToolContext


@pytest.fixture
def feature(tmp_path, monkeypatch):
    package = tmp_path / "test_explainer_feature"
    package.mkdir()
    (package / "__init__.py").write_text("")
    body = (
        '+++\ntopic = "feature"\ntitle = "Feature"\nsummary = "A test feature"\n'
        'aliases = ["example"]\n+++\n\n'
        + "\n\n".join(
            f"## {name}\nMaintained {name.lower()} evidence."
            for name in ("Overview", "Workflow", "Storage", "Safety", "Limitations", "Verification")
        )
    )
    (package / "EXPLAIN.md").write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))

    class Feature(BasePlugin):
        name = "feature"
        explainer_package = "test_explainer_feature"
        state = GateState.AVAILABLE

        def tools(self):
            async def operational(ctx):
                raise AssertionError("An explanation must never run the feature")

            return [
                Tool("do_feature", "Do the feature", {"type": "object"}, operational),
                Tool(
                    "secret_tool",
                    "Secret operation",
                    {"type": "object"},
                    operational,
                    admin_only=True,
                ),
            ]

        def tool_gates(self):
            async def gate(ctx):
                return Gate(self.state, "Feature is switched off here.")

            return {"do_feature": ToolGate(user=gate)}

    return Feature()


def app_for(feature=None):
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=AccessPolicy([GROUP.key], []),
        plugins=[] if feature is None else [feature],
        bot_name="testbot",
        group_mentions_only=True,
    )
    return app, platform


def ctx_for(app, *, admin=False):
    return ToolContext(GROUP, user(), False, app.platform, app.stores, is_admin=admin)


async def test_slash_explanations_use_exact_section_without_model_or_actions(feature):
    app, platform = app_for(feature)
    await app.handle(message("/explain@testbot example storage", thread_id="topic1"))
    assert "Maintained storage evidence." in platform.texts[-1]
    assert "Maintained workflow evidence." not in platform.texts[-1]
    assert "sha256:" in platform.texts[-1]
    assert platform.sent[-1].ref.thread_id == "topic1"


async def test_addressing_help_and_unknown_inputs(feature):
    app, platform = app_for(feature)
    await app.handle(message("/explain feature"))
    assert not platform.texts
    await app.handle(message("/explain@someoneelse feature"))
    assert not platform.texts
    await app.handle(message("/help@testbot"))
    assert "/explain@testbot" in platform.texts[-1]
    await app.handle(message("/explain@testbot ../.env"))
    assert "Unknown" in platform.texts[-1]
    assert "Maintained" not in platform.texts[-1]
    await app.handle(message("/explain@testbot feature badsection"))
    assert "overview" in platform.texts[-1]


async def test_framework_overview_lists_only_registered_visible_topics(feature):
    app, platform = app_for(feature)
    await app.handle(message("/explain@testbot"))
    assert "Postgres" not in platform.texts[-1]  # storage is a separately requested section
    assert "feature" in platform.texts[-1]
    assert "expenses" not in platform.texts[-1]


async def test_document_tool_is_read_only_untrusted_and_freshly_gated(feature):
    app, _ = app_for(feature)
    tool = app.tools.get("read_explainer")
    assert tool is not None
    assert not tool.writes
    result = await tool.handler(ctx_for(app), topic="example", section="storage")
    assert result.untrusted and not result.is_error
    assert "Maintained storage evidence." in result.content
    assert "secret_tool" not in result.content
    assert "do_feature: available" in result.content
    feature.state = GateState.OFF
    result = await tool.handler(ctx_for(app), topic="feature")
    assert "do_feature: off" in result.content
    assert "switched off" in result.content
    feature.state = GateState.UNCONFIGURED
    result = await tool.handler(ctx_for(app), topic="feature")
    assert result.is_error and "Maintained" not in result.content
    admin_result = await tool.handler(ctx_for(app, admin=True), topic="feature")
    assert "secret_tool: available" in admin_result.content
    assert "do_feature" not in admin_result.content


async def test_unknown_topic_does_not_reveal_hidden_or_absent_documents(feature):
    app, _ = app_for(feature)
    feature.state = GateState.UNCONFIGURED
    tool = app.tools.get("read_explainer")
    hidden = await tool.handler(ctx_for(app), topic="feature")
    absent = await tool.handler(ctx_for(app), topic="not_installed")
    assert hidden.content == absent.content


async def test_unknown_section_and_oversized_user_arguments_fail_safely(feature):
    app, _ = app_for(feature)
    tool = app.tools.get("read_explainer")
    for arguments in ({"topic": "feature", "section": "../.env"}, {"topic": "x" * 1000}):
        result = await tool.handler(ctx_for(app), **arguments)
        assert result.is_error
        assert "test_explainer_feature" not in result.content
    assert "description" in tool.parameters["properties"]["topic"]
    assert tool.parameters["additionalProperties"] is False


def test_reserved_plugin_name_cannot_be_installed_twice():
    class Duplicate(BasePlugin):
        name = "explain"

    with pytest.raises(ValueError, match="explain"):
        app_for(Duplicate())


async def test_gate_and_variant_hints_use_current_command_spelling(feature):
    app, _ = app_for(feature)
    explain = next(p for p in app.plugins if p.name == "explain")

    async def off(name, ctx):
        return Gate(GateState.OFF, "Use {cmd:feature} on.")

    explain.evaluate_tool = off
    context = ctx_for(app)
    context.command_form = lambda name: f"/{name}@testbot"
    result = await app.tools.get("read_explainer").handler(context, topic="feature")
    assert "/feature@testbot on" in result.content
    assert "{cmd:" not in result.content

    async def available(name, ctx):
        return Gate(GateState.AVAILABLE)

    async def hints(chat, dm):
        return ("Use {cmd:feature} browser on.",)

    explain.evaluate_tool = available
    explain.lookup_gate = lambda name: ToolGate(variant_hints=hints)
    result = await app.tools.get("read_explainer").handler(context, topic="feature")
    assert "/feature@testbot browser on" in result.content


async def test_unicode_document_and_metadata_stay_within_response_limits(feature):
    from chatcore.explainers import ExplainerCatalog, ExplainerDocument, utf16_length

    app, platform = app_for(feature)
    plugin = next(p for p in app.plugins if p.name == "explain")

    async def operational(ctx):
        raise AssertionError("No action may run")

    for i in range(1000):
        app.tools.register(Tool("tool_" + str(i), "Test tool", {"type": "object"}, operational))
    original = plugin.catalog.resolve("feature")
    large = ExplainerDocument(
        replace(original.spec, tools=tuple("tool_" + str(i) for i in range(1000))),
        tuple((key, "😀" * 1250) for key, _ in original.sections),
        original.sha256,
    )
    plugin.catalog = ExplainerCatalog([large])
    await app.handle(message("/explain@testbot feature storage"))
    assert utf16_length(platform.texts[-1]) <= 3500
    assert "😀" * 1250 in platform.texts[-1]
    result = await app.tools.get("read_explainer").handler(ctx_for(app), topic="feature")
    assert len(result.content.encode()) <= 16 * 1024
    assert "omitted" in result.content
    assert json.dumps(result.content)  # no malformed Unicode after shortening


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
async def test_natural_language_retrieves_packaged_evidence_in_every_skill_mode(feature, mode):
    from chatcore.agent import AgentResponder, AgentSettings
    from chatcore.limits import Limits
    from chatcore.llm.types import Completion, Message, ToolCall
    from chatcore.skill_turn import SkillSettings
    from chatcore.skills import SkillsMode
    from chatcore.testing import ScriptedProvider

    app, platform = app_for(feature)
    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant(
                    "",
                    (
                        ToolCall(
                            "read-1",
                            "read_explainer",
                            {
                                "topic": "feature",
                                "section": "storage",
                            },
                        ),
                    ),
                ),
                "tool_use",
            ),
            Completion(Message.assistant("Based on the maintained storage evidence."), "end_turn"),
        ]
    )
    app.responder = AgentResponder(
        provider,
        system_prompt="A helpful bot.",
        limits=Limits(app.stores.cache),
        settings=AgentSettings(
            skills=SkillSettings(
                mode=SkillsMode(mode),
                chats=frozenset({GROUP.key}),
                router="keyword",
            )
        ),
    )
    await app.handle(message("explain internally how feature stores records", mention=True))
    assert len(provider.calls) == 2
    assert any(
        "Maintained storage evidence." in item.text for item in provider.calls[1]["messages"]
    )
    assert platform.texts[-1] == "Based on the maintained storage evidence."


def _with_skill(feature):
    from chatcore.skills import Skill

    feature.skills = lambda: [
        Skill(
            "feature_skill",
            "Do the feature",
            ("feature one", "feature two", "feature three", "feature four"),
            ("do_feature",),
        )
    ]
    return feature


async def test_runtime_availability_precedes_setup_prose_and_is_labelled_current(feature):
    app, _ = app_for(feature)
    result = await app.tools.get("read_explainer").handler(ctx_for(app), topic="feature")
    content = result.content
    assert result.untrusted
    assert content.index("do_feature: available") < content.index("Maintained overview evidence.")
    assert "not the setup defaults" in content
    assert "quota or provider" in content
    # Outside a skills-on agent step nothing is said about offers or loading.
    assert "use_skill" not in content


async def test_available_tool_not_offered_this_step_names_its_loadable_skill(feature):
    app, _ = app_for(_with_skill(feature))
    ctx = ctx_for(app)
    ctx.turn.offered_tools = frozenset({"read_explainer", "use_skill"})
    result = await app.tools.get("read_explainer").handler(ctx, topic="feature")
    line = next(
        line for line in result.content.splitlines() if line.startswith("do_feature: available")
    )
    assert "not in your tools this step" in line
    assert "use_skill" in line and "feature_skill" in line
    assert "secret_tool" not in result.content


async def test_offered_tool_is_reported_as_callable_now_without_a_load_step(feature):
    app, _ = app_for(_with_skill(feature))
    ctx = ctx_for(app)
    ctx.turn.offered_tools = frozenset({"read_explainer", "use_skill", "do_feature"})
    result = await app.tools.get("read_explainer").handler(ctx, topic="feature")
    line = next(
        line for line in result.content.splitlines() if line.startswith("do_feature: available")
    )
    assert "in your tools now" in line
    assert "feature_skill" not in line


async def test_no_load_route_is_suggested_when_use_skill_is_not_offered(feature):
    app, _ = app_for(_with_skill(feature))
    ctx = ctx_for(app)
    ctx.turn.offered_tools = frozenset({"read_explainer"})
    result = await app.tools.get("read_explainer").handler(ctx, topic="feature")
    assert "feature_skill" not in result.content
    assert "not in your tools this step" in result.content


@pytest.mark.parametrize("state", [GateState.OFF, GateState.NOT_ALLOWED])
async def test_off_and_not_allowed_tools_never_get_a_load_route(feature, state):
    app, _ = app_for(_with_skill(feature))
    feature.state = state
    ctx = ctx_for(app)
    ctx.turn.offered_tools = frozenset({"read_explainer", "use_skill"})
    result = await app.tools.get("read_explainer").handler(ctx, topic="feature")
    assert f"do_feature: {state.value}" in result.content
    assert "feature_skill" not in result.content
    assert "switched off" in result.content


async def test_forged_offer_state_cannot_reveal_hidden_or_unconfigured_tools(feature):
    app, _ = app_for(_with_skill(feature))
    ctx = ctx_for(app)
    ctx.turn.offered_tools = frozenset({"secret_tool", "use_skill", "not_a_tool"})
    result = await app.tools.get("read_explainer").handler(ctx, topic="feature")
    assert "secret_tool" not in result.content
    feature.state = GateState.UNCONFIGURED
    result = await app.tools.get("read_explainer").handler(ctx, topic="feature")
    assert result.is_error and "feature_skill" not in result.content


async def test_agent_tells_the_explainer_what_is_offered_each_step(feature):
    from chatcore.agent import AgentResponder, AgentSettings
    from chatcore.limits import Limits
    from chatcore.llm.types import Completion, Message, ToolCall
    from chatcore.skill_turn import SkillSettings
    from chatcore.skills import SkillsMode
    from chatcore.testing import ScriptedProvider

    app, _platform = app_for(_with_skill(feature))

    def read(call_id):
        return ToolCall(call_id, "read_explainer", {"topic": "feature"})

    provider = ScriptedProvider(
        [
            Completion(Message.assistant("", (read("r1"),)), "tool_use"),
            Completion(
                Message.assistant("", (ToolCall("l1", "use_skill", {"name": "feature_skill"}),)),
                "tool_use",
            ),
            Completion(Message.assistant("", (read("r2"),)), "tool_use"),
            Completion(Message.assistant("Explained."), "end_turn"),
        ]
    )
    app.responder = AgentResponder(
        provider,
        system_prompt="A helpful bot.",
        limits=Limits(app.stores.cache),
        settings=AgentSettings(
            skills=SkillSettings(mode=SkillsMode.ON, chats=frozenset({GROUP.key}), router="keyword")
        ),
    )
    await app.handle(message("explain internally how feature works", mention=True))

    def tool_texts(call):
        return [item.text for item in call["messages"] if item.role == "tool"]

    before = "\n".join(tool_texts(provider.calls[1]))
    after = "\n".join(tool_texts(provider.calls[3]))
    assert "load skill feature_skill with use_skill" in before
    assert "do_feature: available — in your tools now" in after
    # Explaining never ran the feature (its handler raises), and the evidence stays untrusted.
    assert "Explained." in _platform.texts[-1]


async def test_command_stays_within_limit_for_maximum_valid_title_source_and_body(feature):
    from chatcore.explainers import (
        ExplainerCatalog,
        ExplainerDocument,
        ExplanationSource,
        utf16_length,
    )

    app, platform = app_for(feature)
    plugin = next(p for p in app.plugins if p.name == "explain")
    original = plugin.catalog.resolve("feature")
    body = "x" * 2500
    largest = ExplainerDocument(
        # A built-in package name, so the identity carries the longest source link.
        replace(original.spec, title="😀" * 100, package="chatcore_image"),
        tuple((key, body) for key, _ in original.sections),
        original.sha256,
    )
    plugin.catalog = ExplainerCatalog(
        [largest], source=ExplanationSource("a" * 39 + "/" + "b" * 100, "a" * 40)
    )
    await app.handle(message("/explain@testbot feature verification"))
    text = platform.texts[-1]
    assert utf16_length(text) <= 3500
    # The maintained evidence, its source and the live status are all kept; only the fixed
    # wrapper wording is shortened.
    assert body in text
    assert "Source: https://github.com/" in text and "sha256:" in text
    assert "omitted" not in text
    assert text.index("do_feature: available") < text.index(body)
    assert "handlers still validate each operation" in text
    result = await app.tools.get("read_explainer").handler(ctx_for(app), topic="feature")
    assert len(result.content.encode()) <= 16 * 1024
    assert "not the setup defaults" in result.content
