"""Bounded history pages can recover text hidden by serialized excerpt limits."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from chatcore.embed import FakeEmbedder
from chatcore.events import ChatRef
from chatcore.store.base import SearchHit
from chatcore.testing import GROUP, FakePlatform, memory_stores, message, user
from chatcore.tools.registry import ToolContext, ToolRegistry
from chatcore_search import SearchPlugin


async def setup(text, *, is_bot=False, thread_id=None):
    stores = memory_stores()
    if is_bot:
        await stores.repo.log_bot_message(GROUP, "history-source", text, thread_id=thread_id)
    else:
        incoming = replace(message(text, thread_id=thread_id), message_id="history-source")
        await stores.repo.log_message(incoming)
    ctx = ToolContext(GROUP, user(), False, FakePlatform(), stores)
    plugin = SearchPlugin(FakeEmbedder())
    return plugin, ctx


def page(result):
    assert not result.is_error, result.content
    metadata, record = result.content.split("\n", 1)
    assert len(record) <= 2000
    return json.loads(metadata), json.loads(record)


async def test_unicode_history_can_be_reconstructed_without_gaps_or_a_search_index():
    text = "\n".join(f"D{day} 香港行程 🧭 <details> " + "精彩内容 " * 90 for day in range(1, 9))
    text += " literal …[truncated]"
    plugin, ctx = await setup(text)
    offset, restored, pages = 0, "", 0
    while True:
        result = await ToolRegistry(plugin.tools()).call(
            "read_chat_history", {"message_id": "history-source", "offset": offset}, ctx
        )
        assert result.untrusted and not result.persist_content
        meta, record = page(result)
        assert meta["offset"] == offset and meta["total_chars"] == len(text)
        next_offset = meta["next_offset"]
        count = len(text) - offset if next_offset is None else next_offset - offset
        assert count > 0
        restored += record["text"][:count]
        pages += 1
        if next_offset is None:
            break
        assert next_offset > offset
        offset = next_offset
        assert pages < 100
    assert restored == text
    assert "D8" in restored and pages > 1


@pytest.mark.parametrize("offset", [-1, True, 1.5, "1", 1000])
async def test_history_rejects_invalid_text_offsets(offset):
    plugin, ctx = await setup("short")
    result = await plugin.read_chat_history(ctx, message_id="history-source", offset=offset)
    assert result.is_error


@pytest.mark.parametrize("kind", ["missing", "chat", "topic", "unthreaded", "message_id"])
async def test_exact_history_message_is_scoped_even_if_repository_returns_wrong_row(kind):
    plugin, ctx = await setup("private source")
    source = await ctx.stores.repo.get_message(GROUP, "history-source")
    if kind == "missing":
        source = None
    elif kind == "chat":
        source = replace(source, chat=ChatRef("fake", "elsewhere"))
    elif kind == "topic":
        source = replace(source, thread_id="elsewhere")
    elif kind == "unthreaded":
        ctx.thread_id = "topic"
    else:
        source = replace(source, message_id="wrong-message")
    ctx.stores.repo.get_message = AsyncMock(return_value=source)
    result = await plugin.read_chat_history(ctx, message_id="history-source")
    assert result.is_error and "private source" not in result.content


async def test_paging_bot_answers_is_explicit():
    plugin, ctx = await setup("D8 airport", is_bot=True)
    denied = await plugin.read_chat_history(ctx, message_id="history-source")
    assert denied.is_error
    result = await plugin.read_chat_history(
        ctx, message_id="history-source", include_bot_answers=True
    )
    meta, record = page(result)
    assert meta["next_offset"] is None
    assert record["role"] == "assistant" and record["text"] == "D8 airport"


@pytest.mark.parametrize("has_source", [False, True])
async def test_search_snippets_identify_coverage_and_offer_only_real_message_continuations(
    has_source,
):
    text = "Trip plan " * 500 + "D8 airport"
    plugin, ctx = await setup(text)

    class Index:
        async def search(self, *_args, **_kwargs):
            return [
                SearchHit(
                    "indexed-hit",
                    1,
                    {"text": text, "message_ids": ["history-source"] if has_source else []},
                )
            ]

    ctx.stores.search = Index()
    result = await plugin.read_chat_history(ctx, query="Trip")
    first_line = result.content.split("\n", 1)[0]
    metadata = json.loads(first_line[first_line.index("{") :])
    assert metadata["truncated"]
    if has_source:
        assert metadata["message_id"] == "history-source"
        assert metadata["next_offset"] > 0
        continuation = await plugin.read_chat_history(
            ctx, message_id="history-source", offset=metadata["next_offset"]
        )
        continued_meta, _record = page(continuation)
        assert continued_meta["offset"] == metadata["next_offset"]
    else:
        assert metadata["message_id"] is None and metadata["next_offset"] is None
        assert metadata["coverage"] == "indexed excerpt only"
