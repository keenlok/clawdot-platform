from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from chatcore.embed import FakeEmbedder
from chatcore.events import ChatRef, Mention, User, UserRef
from chatcore.llm.types import Completion, Message, Usage
from chatcore.plugin import CommandContext
from chatcore.store.base import SearchHit, Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import FakePlatform, ScriptedProvider, message, user
from chatcore.tools.registry import ToolContext, ToolRegistry
from chatcore_memory.plugin import SENSITIVE_PATTERNS, MemoryPlugin, MemorySettings
from chatcore_memory.repository import PostgresMemoryRepo


class MemorySearch:
    supports_knn = True

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.chats: dict[str, str] = {}
        self.fail_index = False

    async def index(self, index, doc_id, doc, *, chat):
        if self.fail_index:
            raise ConnectionError("search unavailable")
        self.docs[doc_id] = dict(doc)
        self.chats[doc_id] = chat.key

    async def search(self, index, *, chat, text=None, vector=None, filters=None, size=10):
        docs = [(key, doc) for key, doc in self.docs.items() if self.chats[key] == chat.key]
        if text:
            words = set(text.casefold().split())
            docs = [(key, doc) for key, doc in docs if words & set(doc["text"].casefold().split())]
        return [SearchHit(key, 1.0, doc) for key, doc in docs[:size]]

    async def delete(self, index, *, chat, query=None, refresh_first=False):
        term = (query or {}).get("term", {})
        doomed = [
            key
            for key, doc in self.docs.items()
            if self.chats[key] == chat.key and all(doc.get(k) == v for k, v in term.items())
        ]
        for key in doomed:
            self.docs.pop(key)
            self.chats.pop(key)
        return len(doomed)


def context(chat: ChatRef, stores: Stores | None = None) -> ToolContext:
    actor = User(UserRef(chat.platform, "alice"), "Alice", "alice")
    return ToolContext(chat, actor, False, FakePlatform(), stores or stores_for_test())


def stores_for_test(search=None) -> Stores:
    return Stores(repo=MemoryCoreRepo(), cache=MemoryCache(), search=search)


def tool(plugin: MemoryPlugin, name: str):
    item = next(item for item in plugin.tools() if item.name == name)
    if name not in {"remember", "forget"}:
        return item

    async def commit_on_clean_turn(ctx, **args):
        result = await item.handler(ctx, **args)
        if result.pending_action and not ctx.turn.read_untrusted:
            command = CommandContext(
                ctx.chat,
                ctx.user,
                ctx.is_dm,
                False,
                ctx.platform,
                ctx.stores,
                thread_id=ctx.thread_id,
            )
            text = await plugin.action_handlers()[result.pending_action.kind].commit(
                result.pending_action, command
            )
            return type(result)(content=text)
        return result

    return type("ToolForTest", (), {"handler": staticmethod(commit_on_clean_turn)})()


def raw_tool(plugin: MemoryPlugin, name: str):
    return next(item for item in plugin.tools() if item.name == name)


async def test_memories_are_isolated_for_recall_listing_and_id_delete():
    plugin = MemoryPlugin()
    chat_a, chat_b = ChatRef("fake", "a"), ChatRef("fake", "b")
    ctx_a, ctx_b = context(chat_a), context(chat_b)
    saved = await tool(plugin, "remember").handler(ctx_a, fact="Alice likes tea")
    memory_id = int(saved.content.split("#", 1)[1].split(":", 1)[0])

    assert (
        await tool(plugin, "recall").handler(ctx_b, query="Alice tea", limit=5)
    ).content == "No matching memories."
    assert (
        await plugin.commands()[0].handler(
            CommandContext(chat_b, ctx_b.user, False, False, ctx_b.platform, ctx_b.stores), ""
        )
    ) == "I don't remember anything about this chat yet."
    gone = await tool(plugin, "forget").handler(ctx_b, memory_id=memory_id)
    assert gone.is_error and gone.content == f"No memory #{memory_id} in this chat."


async def test_untrusted_turn_requires_confirmation_and_handler_commits():
    plugin = MemoryPlugin()
    ctx = context(ChatRef("fake", "a"))
    ctx.turn.read_untrusted = True
    result = await tool(plugin, "remember").handler(ctx, fact="We meet on Fridays")
    assert result.pending_action and result.pending_action.kind == "remember"
    assert result.pending_action.summary.endswith('"We meet on Fridays"')
    listing = await plugin.commands()[0].handler(
        CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores), ""
    )
    assert listing == "I don't remember anything about this chat yet."
    cmd = CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores)
    await plugin.action_handlers()["remember"].commit(result.pending_action, cmd)
    assert "We meet on Fridays" in (await plugin.commands()[0].handler(cmd, ""))


@pytest.mark.asyncio
async def test_clean_remember_is_pending_until_commit_and_uses_explicit_actor():
    plugin = MemoryPlugin()
    ctx = context(ChatRef("fake", "a"))
    item = raw_tool(plugin, "remember")
    assert item.writes and item.direct
    result = await item.handler(ctx, fact="We meet on Mondays")
    assert result.pending_action is not None
    assert await plugin._repo(ctx.stores).list(ctx.chat) == []
    result.pending_action.payload.update(source="auto", created_by="fake:other")
    command = CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores)
    echo = await plugin.action_handlers()["remember"].commit(result.pending_action, command)
    assert echo == 'Saved memory #1: "We meet on Mondays". Say "undo" to forget it.'
    row = await plugin._repo(ctx.stores).get(ctx.chat, 1)
    assert row["source"] == "explicit" and row["created_by"] == ctx.user.ref.key


@pytest.mark.asyncio
async def test_clean_forget_is_pending_until_commit_and_echoes_deleted_text():
    plugin = MemoryPlugin()
    ctx = context(ChatRef("fake", "a"))
    saved = await tool(plugin, "remember").handler(ctx, fact="A quiet surprise")
    memory_id = int(saved.content.split("#", 1)[1].split(":", 1)[0])
    item = raw_tool(plugin, "forget")
    assert item.writes and item.direct
    pending = await item.handler(ctx, memory_id=memory_id)
    assert pending.pending_action is not None
    assert await plugin._repo(ctx.stores).get(ctx.chat, memory_id) is not None
    command = CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores)
    echo = await plugin.action_handlers()["forget"].commit(pending.pending_action, command)
    assert echo == (
        f'Forgot memory #{memory_id}: "A quiet surprise". Older pending memory saves '
        'are now out of date and need to be requested again. Say "undo" to save it again.'
    )
    assert await plugin._repo(ctx.stores).get(ctx.chat, memory_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["source", "created_by"])
async def test_untrusted_remember_rejects_internal_save_arguments(argument):
    plugin = MemoryPlugin()
    ctx = context(ChatRef("fake", "a"))
    ctx.turn.read_untrusted = True
    args = {"fact": "Injected fact", argument: "auto"}
    registry = ToolRegistry(plugin.tools())
    result = await registry.call("remember", args, ctx)
    assert result.is_error and argument in result.content
    assert await plugin._repo(ctx.stores).list(ctx.chat) == []


@pytest.mark.asyncio
async def test_forget_confirmation_does_not_delete_until_commit():
    plugin = MemoryPlugin()
    ctx = context(ChatRef("fake", "a"))
    saved = await tool(plugin, "remember").handler(ctx, fact="Keep this detail")
    memory_id = int(saved.content.split("#", 1)[1].split(":", 1)[0])
    ctx.turn.read_untrusted = True
    pending = await tool(plugin, "forget").handler(ctx, memory_id=memory_id)
    assert pending.pending_action is not None
    assert '"Keep this detail"' in pending.pending_action.summary
    assert (await tool(plugin, "recall").handler(ctx, query="detail")).content.startswith("#")
    cmd = CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores)
    await plugin.action_handlers()["forget"].commit(pending.pending_action, cmd)
    assert (
        await tool(plugin, "recall").handler(ctx, query="detail")
    ).content == "No matching memories."


@pytest.mark.asyncio
async def test_recall_is_untrusted_and_jaccard_dedupe_inserts_without_replacing():
    plugin = MemoryPlugin()
    ctx = context(ChatRef("fake", "a"))
    await tool(plugin, "remember").handler(ctx, fact="Alice prefers green tea")
    result = await tool(plugin, "remember").handler(ctx, fact="Alice prefers green tea please")
    recalled = await tool(plugin, "recall").handler(ctx, query="green tea")
    assert result.content.startswith('Saved memory #2: "Alice prefers green tea please"')
    assert "replaces=1" in result.content
    assert recalled.untrusted
    assert recalled.content.count("#1") == 1


@pytest.mark.asyncio
async def test_tool_and_command_forget_delete_by_id():
    plugin = MemoryPlugin()
    ctx = context(ChatRef("fake", "a"))
    saved = await tool(plugin, "remember").handler(ctx, fact="Forgettable detail")
    memory_id = int(saved.content.split("#", 1)[1].split(":", 1)[0])
    forgotten = await tool(plugin, "forget").handler(ctx, memory_id=memory_id)
    assert forgotten.content == (
        f'Forgot memory #{memory_id}: "Forgettable detail". Older pending memory saves '
        'are now out of date and need to be requested again. Say "undo" to save it again.'
    )
    saved = await tool(plugin, "remember").handler(ctx, fact="Another forgettable detail")
    memory_id = int(saved.content.split("#", 1)[1].split(":", 1)[0])
    command_ctx = CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores)
    assert await plugin.commands()[1].handler(command_ctx, str(memory_id)) == (
        f"Forgot memory #{memory_id}."
    )
    bad = await plugin.commands()[1].handler(command_ctx, "bad")
    assert bad.awaits_reply and "memory number" in bad.text
    assert await plugin.commands()[1].handler(command_ctx, "") == (
        "I don't remember anything about this chat yet."
    )
    await tool(plugin, "remember").handler(ctx, fact="Kept detail")
    bare = await plugin.commands()[1].handler(command_ctx, "")
    assert bare.awaits_reply and "Kept detail" in bare.text
    assert "number of the memory to forget" in bare.text


@pytest.mark.asyncio
async def test_auto_extraction_sees_only_human_text_and_filters_sensitive_facts():
    provider = ScriptedProvider(
        responses=[
            Completion(
                Message.assistant(
                    json.dumps(
                        {
                            "facts": [
                                {"text": "We meet every Friday", "about_speaker": False},
                                {"text": "Alice likes board games", "about_speaker": True},
                                {"text": "My password is safe", "about_speaker": True},
                            ]
                        }
                    )
                ),
                "end_turn",
                Usage(8, 10),
            )
        ]
    )
    plugin = MemoryPlugin(provider=provider)
    msg = message("Our standing plan is to meet every Friday after work.")
    stores = stores_for_test()
    await stores.repo.log_message(msg)
    await plugin.after_turn(msg, msg.text, stores)
    assert json.loads(provider.calls[0]["messages"][0].text)["untrusted"] == msg.text
    cmd = CommandContext(msg.chat, msg.user, False, False, FakePlatform(), stores)
    listing = await plugin.commands()[0].handler(cmd, "")
    assert "We meet every Friday" in listing
    assert "board games" in listing
    assert "password" not in listing
    assert listing.count("[auto]") == 2


@pytest.mark.asyncio
async def test_iso_date_range_survives_phone_sensitive_filter():
    provider = ScriptedProvider(
        responses=[
            Completion(
                Message.assistant(
                    json.dumps(
                        {
                            "facts": [
                                {
                                    "text": "Trip to Japan 2026-12-12 to 2026-12-20",
                                    "about_speaker": False,
                                }
                            ]
                        }
                    )
                ),
                "end_turn",
                Usage(2, 4),
            )
        ]
    )
    plugin = MemoryPlugin(provider=provider)
    msg = message("Our trip to Japan will run from 2026-12-12 to 2026-12-20 every year.")
    stores = stores_for_test()
    await stores.repo.log_message(msg)
    await plugin.after_turn(msg, msg.text, stores)
    assert "2026-12-12" in (
        await plugin.commands()[0].handler(
            CommandContext(msg.chat, msg.user, False, False, FakePlatform(), stores), ""
        )
    )


@pytest.mark.asyncio
async def test_extraction_strips_surrounding_json_code_fence():
    provider = ScriptedProvider(
        responses=[
            Completion(
                Message.assistant(
                    '```json\n{"facts":[{"text":"We meet each Friday","about_speaker":false}]}\n```'
                ),
                "end_turn",
                Usage(2, 4),
            )
        ]
    )
    plugin = MemoryPlugin(provider=provider)
    msg = message("Our weekly standing plan is to meet each Friday.")
    stores = stores_for_test()
    await stores.repo.log_message(msg)
    await plugin.after_turn(msg, msg.text, stores)
    assert "We meet each Friday" in (
        await plugin.commands()[0].handler(
            CommandContext(msg.chat, msg.user, False, False, FakePlatform(), stores), ""
        )
    )


@pytest.mark.asyncio
async def test_extraction_parses_json_surrounded_by_prose():
    provider = ScriptedProvider(
        responses=[
            Completion(
                Message.assistant(
                    "Here are the durable facts:\n"
                    '{"facts":[{"text":"We meet each Friday","about_speaker":false}]}\n'
                    "That is all."
                ),
                "end_turn",
                Usage(2, 4),
            )
        ]
    )
    plugin = MemoryPlugin(provider=provider)
    msg = message("Our weekly standing plan is to meet each Friday.")
    stores = stores_for_test()
    await stores.repo.log_message(msg)
    await plugin.after_turn(msg, msg.text, stores)
    rows = await plugin._repo(stores).list(msg.chat)
    assert [row["text"] for row in rows] == ["We meet each Friday"]


@pytest.mark.asyncio
async def test_extraction_accepts_prose_with_unmatched_brace_after_json():
    provider = ScriptedProvider(
        responses=[
            Completion(
                Message.assistant('{"facts":[{"text":"We meet each Friday"}]} More context: {'),
                "end_turn",
                Usage(2, 4),
            )
        ]
    )
    plugin = MemoryPlugin(provider=provider)
    msg = message("Our weekly standing plan is to meet each Friday.")
    stores = stores_for_test()
    await stores.repo.log_message(msg)
    await plugin.after_turn(msg, msg.text, stores)
    rows = await plugin._repo(stores).list(msg.chat)
    assert [row["text"] for row in rows] == ["We meet each Friday"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("", "empty"),
        ("not JSON", "invalid_json"),
        ('{"facts":[{"text":"An incomplete fact"}', "truncated"),
        ('{"items":[]}', "wrong_schema"),
        ('{"wrapper":{"facts":[{"text":"Nested fact"}]}}', "wrong_schema"),
        ('[{"facts":[{"text":"Nested fact"}]}]', "wrong_schema"),
        ('{"wrapper":{"facts":[{"text":"Nested fact"}]}', "truncated"),
    ],
)
async def test_unusable_extraction_response_logs_reason_and_saves_nothing(reply, reason, caplog):
    provider = ScriptedProvider(
        responses=[Completion(Message.assistant(reply), "end_turn", Usage(2, 2))]
    )
    plugin = MemoryPlugin(provider=provider)
    msg = message("This is a long enough ordinary message.")
    stores = stores_for_test()
    await stores.repo.log_message(msg)
    await plugin.after_turn(msg, msg.text, stores)
    assert await plugin._repo(stores).list(msg.chat) == []
    assert f"reason={reason}" in caplog.text


@pytest.mark.asyncio
async def test_empty_facts_array_is_a_valid_extraction():
    provider = ScriptedProvider(
        responses=[Completion(Message.assistant('{"facts":[]}'), "end_turn", Usage(2, 2))]
    )
    plugin = MemoryPlugin(provider=provider)
    msg = message("This is a long enough ordinary message.")
    stores = stores_for_test()
    await stores.repo.log_message(msg)
    await plugin.after_turn(msg, msg.text, stores)
    assert await plugin._repo(stores).list(msg.chat) == []


@pytest.mark.asyncio
async def test_max_tokens_refuses_facts_after_recording_completion_usage():
    class RecordingLimits:
        class settings:
            DAILY_TOKEN_BUDGET = 100

        def __init__(self):
            self.recorded = []

        async def budget_used(self):
            return 0

        async def budget_exhausted(self):
            return False

        async def record(self, tokens):
            self.recorded.append(tokens)

    limits = RecordingLimits()
    provider = ScriptedProvider(
        responses=[
            Completion(
                Message.assistant('{"facts":[{"text":"We meet each Friday"}]}'),
                "max_tokens",
                Usage(7, 11),
            )
        ]
    )
    plugin = MemoryPlugin(provider=provider, limits=limits)
    msg = message("Our weekly standing plan is to meet each Friday.")
    stores = stores_for_test()
    await stores.repo.log_message(msg)
    await plugin.after_turn(msg, msg.text, stores)
    assert await plugin._repo(stores).list(msg.chat) == []
    assert limits.recorded == [18]


@pytest.mark.asyncio
async def test_extraction_uses_trusted_provenance_not_response_fields():
    provider = ScriptedProvider(
        responses=[
            Completion(
                Message.assistant(
                    '{"facts":[{"text":"The group meets weekly",'
                    '"about_speaker":true,"source":"manual",'
                    '"created_by":"forged-user"}]}'
                ),
                "end_turn",
                Usage(2, 4),
            )
        ]
    )
    plugin = MemoryPlugin(provider=provider)
    msg = message("Our weekly standing plan is to meet together.")
    stores = stores_for_test()
    await stores.repo.log_message(msg)
    await plugin.after_turn(msg, msg.text, stores)
    rows = await plugin._repo(stores).list(msg.chat)
    assert len(rows) == 1
    assert rows[0]["source"] == "auto"
    assert rows[0]["created_by"] == msg.user.ref.key
    assert rows[0]["subject_user_key"] == msg.user.ref.key


@pytest.mark.asyncio
async def test_auto_extraction_uses_runtime_budget_override():
    from chatcore.limits import BudgetOverride, Limits, LimitsSettings

    limits = Limits(
        MemoryCache(),
        LimitsSettings(DAILY_TOKEN_BUDGET=100_000, BUDGET_TZ="UTC"),
        budget_override=lambda: BudgetOverride(200_000, None, None, "fake:owner", None),
    )
    await limits.record(150_000)
    provider = ScriptedProvider(
        responses=[
            Completion(
                Message.assistant('{"facts":[{"text":"We meet every Tuesday"}]}'),
                "end_turn",
                Usage(2, 2),
            )
        ]
    )
    plugin = MemoryPlugin(provider=provider, limits=limits)
    msg = message("Our recurring plan is to meet every Tuesday.")
    stores = stores_for_test()
    await stores.repo.log_message(msg)
    await plugin.after_turn(msg, msg.text, stores)
    assert len(provider.calls) == 1
    assert len(await plugin._repo(stores).list(msg.chat)) == 1


@pytest.mark.asyncio
async def test_auto_extraction_skips_bot_command_short_input_and_spent_budget():
    provider = ScriptedProvider()
    plugin = MemoryPlugin(provider=provider)
    stores = stores_for_test()
    await plugin.after_turn(message("/memory now"), "/memory now", stores)
    await plugin.after_turn(message("short"), "short", stores)
    await plugin.after_turn(
        message(
            "This sentence is long enough.", sender=User(UserRef("fake", "bot"), "Bot", is_bot=True)
        ),
        "This sentence is long enough.",
        stores,
    )

    class SpentLimits:
        class settings:
            DAILY_TOKEN_BUDGET = 5

        async def budget_used(self):
            return 5

        async def budget_exhausted(self):
            return True

    plugin.limits = SpentLimits()
    await plugin.after_turn(
        message("A sufficiently long human sentence."),
        "A sufficiently long human sentence.",
        stores,
    )
    assert provider.calls == []


@pytest.mark.asyncio
async def test_before_turn_formats_pinned_and_relevant_memories_and_isolates_chats():
    plugin = MemoryPlugin(settings=MemorySettings(recall_k=3))
    chat_a, chat_b = ChatRef("fake", "a"), ChatRef("fake", "b")
    stores = stores_for_test()
    ctx_a = context(chat_a, stores)
    await tool(plugin, "remember").handler(ctx_a, fact="The team meets at the library", pinned=True)
    await tool(plugin, "remember").handler(ctx_a, fact="The team meets at a different time")
    prompt = await plugin.before_turn(
        message("When does the team meet?", chat=chat_a), "team meets", stores
    )
    assert prompt is not None
    assert "Pinned facts about this chat" in prompt
    assert "Possibly relevant memories" in prompt
    assert (
        await plugin.before_turn(message("team meets", chat=chat_b), "team meets", stores) is None
    )


@pytest.mark.asyncio
async def test_about_resolves_exact_chat_member_and_memory_list_marks_source():
    plugin = MemoryPlugin()
    ctx = context(ChatRef("fake", "a"))
    member = User(UserRef("fake", "bob"), "Bob Smith", "bob")
    await ctx.stores.repo.upsert_member(ctx.chat, member)
    await tool(plugin, "remember").handler(ctx, fact="Bob likes puzzles", about="@bob")
    auto = await plugin._remember(ctx, "The group meets weekly", source="auto")
    assert not auto.is_error
    listing = await plugin.commands()[0].handler(
        CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores), ""
    )
    assert "[auto]" in listing
    assert "subject_user_key" not in listing


@pytest.mark.asyncio
async def test_purge_user_and_chat_keep_other_users_and_chats():
    plugin = MemoryPlugin()
    chat_a, chat_b = ChatRef("fake", "a"), ChatRef("fake", "b")
    stores = stores_for_test()
    ctx_a = context(chat_a, stores)
    ctx_b = context(chat_b, stores)
    bob = UserRef("fake", "bob")
    await plugin._save(
        chat_a,
        "Bob's explicit favorite tea",
        stores,
        source="explicit",
        pinned=False,
        created_by=ctx_a.user.ref,
        subject_user=bob,
    )
    await plugin._save(
        chat_a,
        "Bob mentioned a weekly game",
        stores,
        source="auto",
        pinned=False,
        created_by=bob,
        subject_user=None,
    )
    await tool(plugin, "remember").handler(ctx_a, fact="Alice enjoys chess")
    await tool(plugin, "remember").handler(ctx_b, fact="Other chat's plan")
    await plugin.purge_user(chat_a, bob, stores)
    remaining = await plugin._repo(stores).list(chat_a)
    assert [row["text"] for row in remaining] == ["Alice enjoys chess"]
    await plugin.purge_chat(chat_a, stores)
    assert await plugin._repo(stores).list(chat_a) == []
    assert len(await plugin._repo(stores).list(chat_b)) == 1


@pytest.mark.parametrize(
    "sample",
    [
        "diagnosed with diabetes",
        "$500 cash",
        "card number 1234 5678 1234 5678",
        "password is abc",
        "alice@example.test",
        "+1 555 123 4567",
        "S1234567D",
        "10 Main Street, Singapore",
    ],
)
def test_sensitive_deny_list_covers_each_category(sample: str):
    assert any(pattern.search(sample) for pattern in SENSITIVE_PATTERNS)


@pytest.mark.asyncio
async def test_es_enabled_dedupe_uses_vector_similarity():
    search = MemorySearch()
    embedder = FakeEmbedder()
    plugin = MemoryPlugin(embedder=embedder)
    ctx = context(ChatRef("fake", "a"), stores_for_test(search))
    await tool(plugin, "remember").handler(ctx, fact="Alice likes green tea")
    result = await tool(plugin, "remember").handler(ctx, fact="Alice likes green tea")
    assert result.content == "Already saved as #1."
    assert len(search.docs) == 1


@pytest.mark.asyncio
async def test_vector_dedupe_uses_document_embeddings():
    class DocumentEmbedder:
        dims = 768

        def __init__(self):
            self.inner = FakeEmbedder()
            self.document_calls = 0
            self.query_calls = 0

        async def embed_documents(self, texts):
            self.document_calls += 1
            return await self.inner.embed_documents(texts)

        async def embed_query(self, text):
            self.query_calls += 1
            raise AssertionError("dedupe must use document embeddings")

    embedder, search = DocumentEmbedder(), MemorySearch()
    plugin = MemoryPlugin(embedder=embedder)
    ctx = context(ChatRef("fake", "a"), stores_for_test(search))
    await tool(plugin, "remember").handler(ctx, fact="Alice likes green tea")
    result = await tool(plugin, "remember").handler(ctx, fact="Alice likes green tea")
    assert result.content == "Already saved as #1."
    assert embedder.document_calls >= 3
    assert embedder.query_calls == 0


@pytest.mark.asyncio
async def test_remember_commit_embeds_once_for_dedupe():
    class CountingEmbedder:
        dims = 768

        def __init__(self):
            self.inner = FakeEmbedder()
            self.document_calls = 0

        async def embed_documents(self, texts):
            self.document_calls += 1
            return await self.inner.embed_documents(texts)

        async def embed_query(self, text):
            raise AssertionError("dedupe must use document embeddings")

    embedder, search = CountingEmbedder(), MemorySearch()
    plugin = MemoryPlugin(embedder=embedder)
    ctx = context(ChatRef("fake", "counted"), stores_for_test(search))
    result = await raw_tool(plugin, "remember").handler(ctx, fact="One durable fact")
    command = CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores)
    await plugin.action_handlers()["remember"].commit(result.pending_action, command)

    # One call performs dedupe; the other indexes the saved memory.
    assert embedder.document_calls == 2


@pytest.mark.asyncio
async def test_vector_hit_without_source_embedding_falls_through_to_jaccard():
    search = MemorySearch()
    plugin = MemoryPlugin(embedder=FakeEmbedder())
    ctx = context(ChatRef("fake", "a"), stores_for_test(search))
    await tool(plugin, "remember").handler(ctx, fact="Alice likes green tea")
    search.docs["1"].pop("embedding")
    result = await tool(plugin, "remember").handler(ctx, fact="Alice likes green tea, please")
    assert result.content.startswith('Saved memory #2: "Alice likes green tea, please"')


@pytest.mark.asyncio
async def test_postgres_recall_escapes_like_wildcards():
    class RecordingDatabase:
        async def fetch(self, sql, *args):
            self.sql, self.args = sql, args
            return []

    db = RecordingDatabase()
    repo = PostgresMemoryRepo(db)  # type: ignore[arg-type]
    await repo.match(ChatRef("fake", "a"), r"foo%_bar\baz", limit=5)
    assert db.args[1] == [r"foo\%\_bar\\baz"]
    assert "ESCAPE chr(92)" in db.sql


@pytest.mark.asyncio
async def test_no_search_uses_text_recall_without_embedding():
    class CountingEmbedder:
        dims = 768

        def __init__(self):
            self.calls = 0

        async def embed_query(self, text):
            self.calls += 1
            return [0.0] * self.dims

        async def embed_documents(self, texts):
            self.calls += 1
            return [[0.0] * self.dims for _ in texts]

    embedder = CountingEmbedder()
    plugin = MemoryPlugin(embedder=embedder)
    ctx = context(ChatRef("fake", "a"))
    await tool(plugin, "remember").handler(ctx, fact="A reserved table in Osaka")
    result = await tool(plugin, "recall").handler(ctx, query="table Osaka")
    assert result.untrusted and "Osaka" in result.content
    assert embedder.calls == 0


@pytest.mark.asyncio
async def test_index_failure_keeps_database_row_and_logs_only_error_class(caplog):
    search = MemorySearch()
    search.fail_index = True
    plugin = MemoryPlugin(embedder=FakeEmbedder())
    ctx = context(ChatRef("fake", "a"), stores_for_test(search))
    await tool(plugin, "remember").handler(ctx, fact="Sensitive private phrase")
    assert len(await plugin._repo(ctx.stores).list(ctx.chat)) == 1
    assert "Sensitive private phrase" not in caplog.text
    assert "ConnectionError" in caplog.text


def test_memory_rendering_sanitizes_names_and_marks_unverified():
    from chatcore_memory.service import render_fact, safe_name

    now = datetime(2026, 11, 1, tzinfo=UTC)
    row = {
        "id": 12,
        "text": "likes tea",
        "source": "auto",
        "pinned": False,
        "subject_user_key": "fake:alice",
        "created_by": "fake:bob",
        "created_at": now,
        "updated_at": now.replace(day=2),
    }
    assert safe_name("  Alice\n[Admin]  ") == "AliceAdmin"
    assert render_fact(row, {"fake:alice": "Alice"}) == (
        "#12 [about Alice (fake:alice); saved 2026-11-01; unverified] likes tea"
    )


def test_memory_context_respects_budget_and_skips_duplicates():
    from chatcore_memory.service import build_context

    pinned = [
        {
            "id": 1,
            "text": "A",
            "source": "explicit",
            "pinned": True,
            "subject_user_key": None,
            "created_by": "fake:alice",
            "created_at": datetime(2026, 11, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 11, 1, tzinfo=UTC),
        }
    ]
    assert build_context(pinned, pinned, {}, max_chars=0) == ""
    text = build_context(pinned, [*pinned, {**pinned[0], "id": 2, "text": "B"}], {}, max_chars=500)
    assert text.count("#1") == 1 and "#2" in text


@pytest.mark.asyncio
async def test_replacement_rejects_missing_cross_chat_and_changed_targets():
    plugin = MemoryPlugin()
    ctx = context(ChatRef("fake", "replace-a"))
    missing = await raw_tool(plugin, "remember").handler(
        ctx, fact="new durable detail", replaces=99
    )
    assert missing.is_error and "No memory #99" in missing.content

    other = context(ChatRef("fake", "replace-b"))
    saved = await tool(plugin, "remember").handler(other, fact="Other chat detail")
    other_id = int(saved.content.split("#", 1)[1].split(":", 1)[0])
    cross_chat = await raw_tool(plugin, "remember").handler(
        ctx, fact="Replacement text", replaces=other_id
    )
    assert cross_chat.is_error

    saved = await tool(plugin, "remember").handler(ctx, fact="Original durable detail")
    memory_id = int(saved.content.split("#", 1)[1].split(":", 1)[0])
    pending = await raw_tool(plugin, "remember").handler(
        ctx, fact="Corrected durable detail", replaces=memory_id
    )
    repo = plugin._repo(ctx.stores)
    repo.rows[memory_id]["updated_at"] = repo.rows[memory_id]["updated_at"].replace(
        microsecond=(repo.rows[memory_id]["updated_at"].microsecond + 1) % 1000000
    )
    command = CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores)
    echo = await plugin.action_handlers()["remember"].commit(pending.pending_action, command)
    assert "changed or was removed" in echo
    assert (await plugin._repo(ctx.stores).get(ctx.chat, memory_id))[
        "text"
    ] == "Original durable detail"


@pytest.mark.asyncio
async def test_replacement_preserves_pinned_and_subject_and_requires_resolved_about():
    plugin = MemoryPlugin()
    ctx = context(ChatRef("fake", "replace-pinned"))
    member = User(UserRef("fake", "bob"), "Bob", "bob")
    await ctx.stores.repo.upsert_member(ctx.chat, member)
    saved = await tool(plugin, "remember").handler(
        ctx, fact="Bob's weekly chess club", about="Bob", pinned=True
    )
    memory_id = int(saved.content.split("#", 1)[1].split(":", 1)[0])
    pending = await raw_tool(plugin, "remember").handler(
        ctx, fact="Bob's weekly board game club", replaces=memory_id
    )
    command = CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores)
    echo = await plugin.action_handlers()["remember"].commit(pending.pending_action, command)
    assert "Bob's weekly chess club" in echo and "Bob's weekly board game club" in echo
    row = (await plugin._repo(ctx.stores).list(ctx.chat))[0]
    assert row["pinned"] and row["subject_user_key"] == member.ref.key

    pending_bad_about = await raw_tool(plugin, "remember").handler(
        ctx, fact="Another corrected detail", about="Nobody", replaces=int(row["id"])
    )
    echo = await plugin.action_handlers()["remember"].commit(
        pending_bad_about.pending_action, command
    )
    assert "existing memory was kept" in echo
    assert await plugin._repo(ctx.stores).get(ctx.chat, int(row["id"])) is not None


@pytest.mark.asyncio
async def test_automatic_exact_and_near_deduplication_respect_identity_and_keep_text():
    plugin = MemoryPlugin()
    stores = stores_for_test()
    chat = ChatRef("fake", "identity")
    alice, bob = UserRef("fake", "alice"), UserRef("fake", "bob")
    first = await plugin._save(
        chat,
        "Likes green tea.",
        stores,
        source="auto",
        pinned=False,
        created_by=alice,
        subject_user=alice,
    )
    exact = await plugin._save(
        chat,
        "likes GREEN tea",
        stores,
        source="auto",
        pinned=False,
        created_by=bob,
        subject_user=alice,
    )
    second = await plugin._save(
        chat,
        "likes green tea",
        stores,
        source="auto",
        pinned=False,
        created_by=bob,
        subject_user=bob,
    )
    assert first["status"] == "saved" and exact["status"] == "exact"
    assert second["status"] == "saved"
    assert len(await plugin._repo(stores).list(chat)) == 2

    alice_source = message("The group meets every Friday", sender=user("alice"), chat=chat)
    bob_source = message("The group meets every Friday", sender=user("bob"), chat=chat)
    await stores.repo.log_message(alice_source)
    await stores.repo.log_message(bob_source)
    null_first = await plugin._save(
        chat,
        "The group meets every Friday",
        stores,
        source="auto",
        pinned=False,
        created_by=alice,
        epoch=0,
        source_ids=[(alice_source.message_id, "statement")],
        provenance="known",
    )
    null_exact = await plugin._save(
        chat,
        "The group meets every Friday.",
        stores,
        source="auto",
        pinned=False,
        created_by=bob,
        epoch=0,
        source_ids=[(bob_source.message_id, "statement")],
        provenance="known",
    )
    null_alice_again = await plugin._save(
        chat, "The group meets every Friday!", stores, source="auto", pinned=False, created_by=alice
    )
    assert null_first["status"] == "saved" and null_exact["status"] == "exact"
    assert null_alice_again["status"] == "exact"
    assert null_alice_again["row"]["id"] == null_first["row"]["id"]
    assert len(await plugin._repo(stores).list(chat)) == 3
    supports = plugin._repo(stores).sources[null_first["row"]["id"]]
    assert {item["source_author_key"] for item in supports} == {alice.key, bob.key}


@pytest.mark.asyncio
async def test_automatic_extraction_skips_synthetic_and_drops_unlinked_labels():
    from chatcore.threads import TurnOrigin, current_turn_origin

    stores = stores_for_test()
    provider = ScriptedProvider(
        responses=[Completion(Message.assistant('{"facts":[]}'), "end_turn", Usage(1, 1))]
    )
    plugin = MemoryPlugin(provider=provider)
    msg = message("A sufficiently long message for synthetic extraction skip.")
    token = current_turn_origin.set(TurnOrigin("synthetic"))
    try:
        await plugin.after_turn(msg, msg.text, stores)
    finally:
        current_turn_origin.reset(token)
    assert not provider.calls
    raw = await stores.cache.get(
        "memory_metrics:" + datetime.now(UTC).date().isoformat() + ":extract_skipped_synthetic"
    )
    assert int(raw or b"0") == 1


def test_rendering_covers_null_subjects_unknown_names_and_hostile_display_names():
    from chatcore_memory.service import render_fact, safe_name

    stamp = datetime(2026, 11, 1, tzinfo=UTC)
    base = {
        "id": 7,
        "text": "prefers tea",
        "pinned": False,
        "created_at": stamp,
        "updated_at": stamp,
        "subject_user_key": None,
        "created_by": "fake:bob",
    }
    assert render_fact({**base, "source": "auto"}, {"fake:bob": "Bob"}) == (
        "#7 [from Bob; saved 2026-11-01] prefers tea"
    )
    assert render_fact({**base, "source": "explicit"}, {"fake:bob": "Bob"}) == (
        "#7 [saved by Bob; saved 2026-11-01] prefers tea"
    )
    assert render_fact({**base, "source": "explicit", "subject_user_key": "gone:user"}, {}) == (
        "#7 [about unknown person (gone:user); saved 2026-11-01] prefers tea"
    )
    assert safe_name("  Alice [admin]\n\t(Bob)  ") == "Alice adminBob"
    assert len(safe_name("長" * 50)) == 40


def test_context_budget_keeps_whole_unicode_facts_in_deterministic_order():
    from chatcore_memory.service import build_context

    stamp = datetime(2026, 11, 1, tzinfo=UTC)

    def row(memory_id, text, pinned=False):
        return {
            "id": memory_id,
            "text": text,
            "source": "explicit",
            "pinned": pinned,
            "subject_user_key": None,
            "created_by": "fake:alice",
            "created_at": stamp,
            "updated_at": stamp,
        }

    pinned = [row(2, "喜歡茶", True), row(1, "每週見面", True)]
    recalled = [row(1, "duplicate"), row(3, "第一個"), row(4, "第二個")]
    all_text = build_context(pinned, recalled, {"fake:alice": "Alice"}, max_chars=1000)
    assert all_text.index("#2") < all_text.index("#1") < all_text.index("#3") < all_text.index("#4")
    assert all_text.count("#1") == 1
    pinned_only = build_context(
        pinned,
        recalled,
        {"fake:alice": "Alice"},
        max_chars=len(build_context(pinned, [], {"fake:alice": "Alice"}, max_chars=1000)),
    )
    assert "#2" in pinned_only and "#3" not in pinned_only


@pytest.mark.asyncio
async def test_vector_candidate_from_another_subject_cannot_dedupe():
    search = MemorySearch()
    plugin = MemoryPlugin(embedder=FakeEmbedder())
    stores = stores_for_test(search)
    chat = ChatRef("fake", "vector-identity")
    alice, bob = UserRef("fake", "alice"), UserRef("fake", "bob")
    await plugin._save(
        chat,
        "Likes green tea",
        stores,
        source="auto",
        pinned=False,
        created_by=alice,
        subject_user=alice,
    )
    result = await plugin._save(
        chat,
        "Likes green tea",
        stores,
        source="auto",
        pinned=False,
        created_by=bob,
        subject_user=bob,
    )
    assert result["status"] == "saved"
    rows = await plugin._repo(stores).list(chat)
    assert len(rows) == 2 and {row["subject_user_key"] for row in rows} == {alice.key, bob.key}


@pytest.mark.asyncio
async def test_identity_scan_checks_rows_beyond_first_500_and_never_rewrites():
    plugin = MemoryPlugin()
    stores = stores_for_test()
    chat, alice = ChatRef("fake", "old-row"), UserRef("fake", "alice")
    original = await plugin._save(
        chat,
        "The group meets every Friday evening",
        stores,
        source="auto",
        pinned=False,
        created_by=alice,
        subject_user=alice,
    )
    repo = plugin._repo(stores)
    for number in range(501):
        await repo.insert(
            chat,
            f"Unrelated standing detail number {number}",
            source="auto",
            pinned=False,
            created_by=alice,
            subject_user=alice,
        )
    result = await plugin._save(
        chat,
        "The group meets every Friday evening after",
        stores,
        source="auto",
        pinned=False,
        created_by=alice,
        subject_user=alice,
    )
    assert result["status"] == "near"
    assert (await repo.get(chat, int(original["row"]["id"])))["text"] == (
        "The group meets every Friday evening"
    )
    assert len(await repo.list(chat, limit=600)) == 502


@pytest.mark.asyncio
async def test_concurrent_no_database_exact_writes_reuse_one_row():
    import asyncio

    plugin = MemoryPlugin()
    stores = stores_for_test()
    chat, alice = ChatRef("fake", "local-race"), UserRef("fake", "alice")
    results = await asyncio.gather(
        plugin._save(
            chat,
            "The group meets every Friday",
            stores,
            source="auto",
            pinned=False,
            created_by=alice,
        ),
        plugin._save(
            chat,
            "The group meets every Friday",
            stores,
            source="auto",
            pinned=False,
            created_by=alice,
        ),
    )
    assert {result["status"] for result in results} == {"saved", "exact"}
    assert len(await plugin._repo(stores).list(chat)) == 1


@pytest.mark.asyncio
async def test_extraction_subject_labels_map_to_verified_mentions_only():
    speaker = User(UserRef("fake", "alice"), "Alice", "alice")
    bob = User(UserRef("fake", "bob"), "Bob", "bob")
    msg = replace(
        message("Bob prefers the window seat when traveling.", sender=speaker),
        mentions=(
            Mention(
                0,
                4,
                "text_mention",
                "Bob",
                target=bob.ref,
                username=bob.username,
                display_name=bob.display_name,
            ),
        ),
    )
    provider = ScriptedProvider(
        responses=[
            Completion(
                Message.assistant(
                    json.dumps(
                        {
                            "facts": [
                                {
                                    "text": "prefers the window seat when traveling",
                                    "about_label": "P1",
                                },
                                {"text": "someone likes the aisle", "about_other": True},
                            ]
                        }
                    )
                ),
                "end_turn",
                Usage(2, 3),
            )
        ]
    )
    stores = stores_for_test()
    await stores.repo.upsert_member(msg.chat, speaker)
    await stores.repo.upsert_member(msg.chat, bob)
    plugin = MemoryPlugin(provider=provider)
    await stores.repo.log_message(msg)
    await plugin.after_turn(msg, msg.text, stores)
    rows = await plugin._repo(stores).list(msg.chat)
    assert len(rows) == 1 and rows[0]["subject_user_key"] == bob.ref.key
    raw = await stores.cache.get(
        "memory_metrics:" + datetime.now(UTC).date().isoformat() + ":fact_dropped_unlinked"
    )
    assert int(raw or b"0") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("about", ["", "   ", "\t\n"])
async def test_blank_about_replacement_preserves_existing_subject(about):
    plugin = MemoryPlugin()
    ctx = context(ChatRef("fake", "blank-about"))
    bob = User(UserRef("fake", "bob"), "Bob", "bob")
    await ctx.stores.repo.upsert_member(ctx.chat, bob)
    saved = await tool(plugin, "remember").handler(
        ctx, fact="Bob prefers morning meetings", about="Bob"
    )
    memory_id = int(saved.content.split("#", 1)[1].split(":", 1)[0])
    pending = await raw_tool(plugin, "remember").handler(
        ctx, fact="Bob prefers early meetings", replaces=memory_id, about=about
    )
    command = CommandContext(ctx.chat, ctx.user, False, False, ctx.platform, ctx.stores)
    await plugin.action_handlers()["remember"].commit(pending.pending_action, command)
    rows = await plugin._repo(ctx.stores).list(ctx.chat)
    assert len(rows) == 1
    assert rows[0]["text"] == "Bob prefers early meetings"
    assert rows[0]["subject_user_key"] == bob.ref.key


@pytest.mark.parametrize("section", ["pinned", "recalled"])
def test_context_budget_keeps_admitted_facts_when_later_fact_overflows(section):
    from chatcore_memory.service import build_context

    stamp = datetime(2026, 11, 1, tzinfo=UTC)
    admitted = {
        "id": 11,
        "text": "Short fact",
        "source": "explicit",
        "pinned": section == "pinned",
        "subject_user_key": None,
        "created_by": "fake:alice",
        "created_at": stamp,
        "updated_at": stamp,
    }
    oversized = {**admitted, "id": 12, "text": "oversized " * 100}
    if section == "pinned":
        full = [admitted, oversized]
        expected = build_context([admitted], [], {}, max_chars=1000)
        actual = build_context(full, [], {}, max_chars=len(expected))
    else:
        full = [admitted, oversized]
        expected = build_context([], [admitted], {}, max_chars=1000)
        actual = build_context([], full, {}, max_chars=len(expected))
    assert actual == expected


@pytest.mark.asyncio
async def test_similar_explicit_insert_is_indexed_for_vector_recall():
    search = MemorySearch()
    plugin = MemoryPlugin(embedder=FakeEmbedder())
    ctx = context(ChatRef("fake", "similar-index"), stores_for_test(search))
    await tool(plugin, "remember").handler(ctx, fact="Alice likes green tea")
    result = await tool(plugin, "remember").handler(ctx, fact="Alice likes green tea please")
    assert result.content.startswith('Saved memory #2: "Alice likes green tea please"')
    assert search.docs["2"]["text"] == "Alice likes green tea please"


@pytest.mark.parametrize(
    ("fact", "expected"),
    [
        ({"about_other": True, "about_speaker": True}, "drop"),
        ({"about_other": True, "about_speaker": True, "about_label": "P1"}, "drop"),
        ({"about_speaker": True, "about_label": "P1"}, "drop"),
        ({"about_other": True}, "drop"),
        ({"about_label": "P9"}, "drop"),
        ({"about_label": None}, None),
        ({"about_label": ""}, None),
        ({"about_label": None, "about_speaker": True}, UserRef("fake", "alice")),
        ({"about_label": None, "about_other": True}, "drop"),
        ({"about_label": "P1"}, UserRef("fake", "bob")),
        ({"about_other": True, "about_label": "P1"}, UserRef("fake", "bob")),
        ({"about_speaker": True}, UserRef("fake", "alice")),
        ({}, None),
        ({"about_speaker": "true"}, None),
        ({"about_other": "true"}, None),
        ({"about_speaker": True, "about_other": "true"}, UserRef("fake", "alice")),
        ({"about_speaker": "true", "about_other": True}, "drop"),
    ],
)
def test_fact_subject_resolution_drops_conflicting_or_unlinked_identity(fact, expected):
    import chatcore_memory.service as memory_service

    resolver = getattr(memory_service, "resolve_fact_subject", None)
    assert callable(resolver), "service subject resolver is missing"
    speaker = UserRef("fake", "alice")
    labels = {"P1": UserRef("fake", "bob")}
    assert resolver(fact, labels, speaker) == expected
