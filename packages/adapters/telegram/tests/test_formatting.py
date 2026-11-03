from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from chatcore.events import ChatRef, MessageRef, UserRef
from chatcore_telegram import TelegramPlatform, TelegramSettings
from pydantic import SecretStr


def platform_and_bot():
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=42)
    platform = TelegramPlatform(TelegramSettings(bot_token=SecretStr("fake-token")))
    platform._application = SimpleNamespace(bot=bot)
    return platform, bot


def entities(payload):
    return [(e.type, e.offset, e.length, e.url, e.language) for e in payload.get("entities", ())]


@pytest.mark.parametrize(
    ("source", "text", "expected"),
    [
        ("Hello **world**!", "Hello world!", [("bold", 6, 5, None, None)]),
        ("😀 **bold**", "😀 bold", [("bold", 3, 4, None, None)]),
        (
            "**bold _inside_** + ~~gone~~",
            "bold inside + gone",
            [
                ("bold", 0, 11, None, None),
                ("italic", 5, 6, None, None),
                ("strikethrough", 14, 4, None, None),
            ],
        ),
        (
            "[Site](https://example.com/a?x=1&y=2)",
            "Site",
            [("text_link", 0, 4, "https://example.com/a?x=1&y=2", None)],
        ),
        ("`**literal** <x>`", "**literal** <x>", [("code", 0, 15, None, None)]),
        (
            "```python\nprint('<x>')\n```",
            "print('<x>')",
            [("pre", 0, 12, None, "python")],
        ),
        (
            "# Heading\n\n- **First**\n- second",
            "Heading\n\n• First\n• second",
            [("bold", 0, 7, None, None), ("bold", 11, 5, None, None)],
        ),
        (r"\*literal\* and snake_case & 2 < 3", "*literal* and snake_case & 2 < 3", []),
        ("<b>literal</b>", "<b>literal</b>", []),
        ("***unfinished", "***unfinished", []),
        ("[empty]()", "empty", []),
        ("[section](#heading)", "section", []),
        ("[file](README.md)", "file", []),
        ("![alt](data:image/png;base64,AAAA)", "alt", []),
        ("[x](https:example.com)", "x", []),
        ("[x](https:/example.com)", "x", []),
        (
            "![a [inner](https://inner.example)](https://image.example)",
            "a inner",
            [("text_link", 0, 7, "https://image.example", None)],
        ),
        (
            "**Use `x` now**",
            "Use x now",
            [("bold", 0, 4, None, None), ("code", 4, 1, None, None), ("bold", 5, 4, None, None)],
        ),
        ("3. one\n4. two", "3. one\n4. two", []),
        (
            "[![alt](https://img.example/p.png)](https://example.com)",
            "alt",
            [("text_link", 0, 3, "https://example.com", None)],
        ),
        (
            "[Use `code`](https://example.com)",
            "Use code",
            [("text_link", 0, 8, "https://example.com", None)],
        ),
        ("# **Heading**", "Heading", [("bold", 0, 7, None, None)]),
        ("> quoted\n> text", "› quoted\n› text", []),
        (
            "| Name | Cost |\n| --- | --- |\n| A | 2 |",
            "Name | Cost\nA | 2",
            [("bold", 0, 4, None, None), ("bold", 7, 4, None, None)],
        ),
    ],
)
async def test_send_renders_markdown_as_native_entities(source, text, expected):
    platform, bot = platform_and_bot()
    await platform.send(ChatRef("tg", "7"), source)
    payload = bot.send_message.await_args.kwargs
    assert payload["text"] == text
    assert entities(payload) == expected
    assert "parse_mode" not in payload
    assert payload["link_preview_options"].is_disabled


async def test_long_formatted_reply_splits_and_rebases_entities():
    platform, bot = platform_and_bot()
    text = "😀" * 2200
    reply = MessageRef(ChatRef("tg", "7"), "9", "12")
    await platform.send(reply.chat, f"**{text}**", reply_to=reply, thread_id="12")
    calls = [c.kwargs for c in bot.send_message.await_args_list]
    assert len(calls) == 2
    assert "".join(c["text"] for c in calls) == text
    assert [len(c["text"].encode("utf-16-le")) // 2 for c in calls] == [4096, 304]
    assert [entities(c) for c in calls] == [
        [("bold", 0, 4096, None, None)],
        [("bold", 0, 304, None, None)],
    ]
    assert calls[0]["reply_parameters"].message_id == 9
    assert "reply_parameters" not in calls[1]
    assert all(c["message_thread_id"] == 12 for c in calls)


async def test_code_block_spans_chunks_without_reinterpreting_its_contents():
    platform, bot = platform_and_bot()
    code = "x" * 4200 + " **not bold**"
    await platform.send(ChatRef("tg", "7"), f"```text\n{code}\n```")
    calls = [c.kwargs for c in bot.send_message.await_args_list]
    assert "".join(c["text"] for c in calls) == code
    assert all(len(c["text"]) <= 4096 for c in calls)
    assert [entities(c) for c in calls] == [
        [("pre", 0, 4096, None, "text")],
        [("pre", 0, len(code) - 4096, None, "text")],
    ]


async def test_edit_uses_the_same_entity_rendering():
    platform, bot = platform_and_bot()
    await platform.edit(MessageRef(ChatRef("tg", "7"), "42"), "**Updated** & `x < 2`")
    payload = bot.edit_message_text.await_args.kwargs
    assert payload["text"] == "Updated & x < 2"
    assert entities(payload) == [("bold", 0, 7, None, None), ("code", 10, 5, None, None)]
    assert payload["link_preview_options"].is_disabled


async def test_confirmation_preserves_exact_text_for_review():
    platform, bot = platform_and_bot()
    prompt = "Save **literal** and snake_case < 5?"
    await platform.confirm(ChatRef("tg", "7"), prompt, "action", UserRef("tg", "7"))
    payload = bot.send_message.await_args.kwargs
    assert payload["text"] == prompt
    assert not payload.get("entities")
    assert not payload.get("parse_mode")


async def test_edit_rejects_text_over_utf16_limit_before_api_call():
    platform, bot = platform_and_bot()
    with pytest.raises(ValueError, match="message limit"):
        await platform.edit(MessageRef(ChatRef("tg", "7"), "42"), "😀" * 2049)
    bot.edit_message_text.assert_not_awaited()
