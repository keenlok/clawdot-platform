import logging

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.limits import Limits
from chatcore.llm.base import ProviderError
from chatcore.llm.sanitize import strip_tool_markup
from chatcore.llm.types import Completion, Message, ToolCall
from chatcore.plugin import BasePlugin
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolResult

DSML = (
    '\n<｜DSML｜ calls>\n<｜DSML｜ invoke name="search_web">\n'
    '<｜DSML｜ parameter name="max_results" string="false">6</｜DSML｜ parameter>\n'
    '<｜DSML｜ parameter name="query" string="true">'
    "REK KL restaurant review price menu 2025 Damansara</｜DSML｜ parameter>\n"
    "</｜DSML｜ invoke>\n</｜DSML｜ calls>\n"
)


def test_strips_exact_production_dsml_block():
    assert strip_tool_markup(DSML) == ("", True)


def test_strips_native_and_ascii_tool_tokens():
    native = (
        "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>search_web\n"
        '```json\n{"query":"x"}\n```<｜tool▁call▁end｜>'
        "<｜tool▁calls▁end｜>"
    )
    assert strip_tool_markup(native) == ("", True)
    assert strip_tool_markup("<|tool_calls_begin|>call<|tool_calls_end|>") == ("", True)


def test_preserves_prose_around_markup_and_removes_blank_tail():
    assert strip_tool_markup("Here's what I found: REK is in Damansara.\n\n" + DSML) == (
        "Here's what I found: REK is in Damansara.",
        True,
    )
    assert strip_tool_markup("First paragraph.\n\n" + DSML + "\n\nSecond paragraph.") == (
        "First paragraph.\n\nSecond paragraph.",
        True,
    )


def test_strips_generic_blocks_and_unclosed_calls():
    assert strip_tool_markup('<tool_call>{"x":1}</tool_call>') == ("", True)
    assert strip_tool_markup('<function_calls><invoke name="x">go</invoke></function_calls>') == (
        "",
        True,
    )
    assert strip_tool_markup('Before.\n<｜DSML｜ calls>\n<｜DSML｜ invoke name="x">') == (
        "Before.",
        True,
    )


def test_plain_markup_like_text_is_untouched():
    text = "<b>bold</b>; a < b > c; |pipes|; `code`"
    assert strip_tool_markup(text) == (text, False)
    for text in (
        "Use <invoke-command> to run the task.",
        "<tool_call_id> is an identifier.",
        "<function_calls_v2> appears in docs.",
        "Pray <invoke the gods\nand keep writing prose.",
    ):
        assert strip_tool_markup(text) == (text, False)


def test_strips_opener_cut_off_before_its_closing_bracket():
    for tail in (
        '<invoke name="search_web"',
        '<tool_call name="search_web"',
        '<function_calls name="x"',
        '<｜DSML｜ invoke name="search_web"',
        '<｜DSML｜ parameter name="query" string="true',
        "<｜tool▁calls▁be",
    ):
        assert strip_tool_markup("Answer. " + tail) == ("Answer.", True), tail


async def make_app(provider, *, tool_steps=8):
    stores = memory_stores()

    async def read(ctx):
        ctx.turn.seen_urls.add("https://source.example/a")
        return ToolResult("read", untrusted=True)

    class Reader(BasePlugin):
        name = "reader"

        def tools(self):
            return [Tool("read", "Read", {"type": "object", "properties": {}}, read)]

    conversation = ConversationPlugin()
    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        bot_name="bot",
        responder=AgentResponder(
            provider,
            system_prompt="rules",
            settings=AgentSettings(tool_steps=tool_steps),
            limits=Limits(stores.cache),
        ),
        plugins=[Reader(), conversation],
    )
    return app, platform, conversation


async def test_agent_retries_markup_only_and_persists_clean_answer(caplog):
    caplog.set_level(logging.INFO)
    provider = ScriptedProvider(
        [
            Completion(Message.assistant(DSML), "end_turn"),
            Completion(Message.assistant("Recovered answer."), "end_turn"),
        ]
    )
    app, platform, conversation = await make_app(provider, tool_steps=1)
    await app.handle(message("question", mention=True))
    assert platform.texts == ["Recovered answer."]
    assert "Tool calls are impossible" in provider.calls[1]["system"]
    assert provider.calls[0]["tool_choice"] == "auto"
    assert "stripped tool markup" in caplog.text
    assert "DSML" not in caplog.text and "search_web" not in caplog.text
    assert "REK KL restaurant review price menu 2025 Damansara" not in caplog.text
    persisted = " ".join(
        row.content.get("text", "") for row in await conversation.store.recent(GROUP, None)
    )
    assert "｜DSML｜" not in persisted


async def test_final_mixed_text_keeps_only_text_without_retry():
    provider = ScriptedProvider(
        [Completion(Message.assistant("Useful text.\n\n" + DSML), "end_turn")]
    )
    app, platform, _conversation = await make_app(provider, tool_steps=0)
    await app.handle(message("question", mention=True))
    assert platform.texts == ["Useful text."]
    assert len(provider.calls) == 1


async def test_final_tool_calls_with_text_are_ignored():
    provider = ScriptedProvider(
        [
            Completion(
                Message.assistant("Plain final text", (ToolCall("ignored", "read", {}),)),
                "tool_use",
            )
        ]
    )
    app, platform, _conversation = await make_app(provider, tool_steps=0)
    await app.handle(message("question", mention=True))
    assert platform.texts == ["Plain final text"]
    assert len(provider.calls) == 1


async def test_no_tools_retry_refusal_uses_fixed_refusal_text():
    provider = ScriptedProvider(
        [
            Completion(Message.assistant(DSML), "end_turn"),
            Completion(Message.assistant("provider refusal detail"), "refusal"),
        ]
    )
    app, platform, _conversation = await make_app(provider, tool_steps=0)
    await app.handle(message("question", mention=True))
    assert platform.texts == ["Sorry, I can't help with that request."]


async def test_whitespace_final_answer_retries():
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("   \n "), "end_turn"),
            Completion(Message.assistant("Plain response"), "end_turn"),
        ]
    )
    app, platform, _conversation = await make_app(provider, tool_steps=0)
    await app.handle(message("question", mention=True))
    assert platform.texts == ["Plain response"]
    assert len(provider.calls) == 2


async def test_real_tool_call_assistant_markup_is_not_persisted():
    provider = ScriptedProvider(
        [
            Completion(Message.assistant(DSML, (ToolCall("real", "read", {}),)), "tool_use"),
            Completion(Message.assistant("Done."), "end_turn"),
        ]
    )
    app, platform, conversation = await make_app(provider, tool_steps=2)
    await app.handle(message("question", mention=True))
    assert platform.texts == ["Done."]
    rows = await conversation.store.recent(GROUP, None)
    assistant_rows = [row for row in rows if row.role == "assistant"]
    assert "｜DSML｜" not in repr([row.content for row in assistant_rows])


async def test_final_tool_calls_are_ignored_and_empty_retry_lists_seen_urls():
    call = ToolCall("ignored", "read", {})
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("", (call,)), "tool_use"),
            Completion(Message.assistant(DSML), "end_turn"),
            Completion(Message.assistant(DSML), "end_turn"),
            Completion(Message.assistant(DSML), "end_turn"),
        ]
    )
    app, platform, _conversation = await make_app(provider, tool_steps=1)
    await app.handle(message("question", mention=True))
    assert platform.texts[0].startswith(
        "I ran out of steps before I could finish an answer. Here's what I found so far:"
    )
    assert "https://source.example/a" in platform.texts[0]
    assert "https://source.example/a" not in repr(provider.calls[1]["messages"][-1].text)
    assert provider.calls[3]["tools"] == []


async def test_final_and_tool_declared_retry_markup_get_no_tools_retry():
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("", (ToolCall("ignored", "read", {}),)), "tool_use"),
            Completion(Message.assistant(DSML), "end_turn"),
            Completion(Message.assistant(DSML), "end_turn"),
            Completion(Message.assistant("Recovered without tools."), "end_turn"),
        ]
    )
    app, platform, _conversation = await make_app(provider, tool_steps=1)
    await app.handle(message("question", mention=True))
    assert platform.texts == ["Recovered without tools."]
    assert len(provider.calls) == 4
    assert provider.calls[2]["tools"]
    assert provider.calls[2]["tool_choice"] == "none"
    assert provider.calls[3]["tools"] == []
    assert "No tools exist in this request" in provider.calls[3]["system"]


async def test_no_tools_retry_provider_error_uses_step_fallback():
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("", (ToolCall("ignored", "read", {}),)), "tool_use"),
            Completion(Message.assistant(DSML), "end_turn"),
            Completion(Message.assistant(DSML), "end_turn"),
        ]
    )
    original_complete = provider.complete

    async def fail_no_tools(messages, **kwargs):
        if len(provider.calls) == 3:
            raise ProviderError("provider rejected request")
        return await original_complete(messages, **kwargs)

    provider.complete = fail_no_tools
    app, platform, _conversation = await make_app(provider, tool_steps=1)
    await app.handle(message("question", mention=True))
    assert platform.texts[0].startswith("I ran out of steps before I could finish an answer.")


async def test_empty_answer_fallback_without_urls_has_new_wording():
    provider = ScriptedProvider(
        [
            Completion(Message.assistant(DSML), "end_turn"),
            Completion(Message.assistant(DSML), "end_turn"),
            Completion(Message.assistant("  "), "end_turn"),
        ]
    )
    app, platform, _conversation = await make_app(provider, tool_steps=0)
    await app.handle(message("question", mention=True))
    assert platform.texts == [
        "Sorry, I couldn't finish an answer this time. Please try again or narrow the question."
    ]
    assert "search limit" not in platform.texts[0]
