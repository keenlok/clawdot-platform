from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.embed import FakeEmbedder
from chatcore.events import ChatRef, IncomingMessage, Mention, User, UserRef
from chatcore.limits import Limits
from chatcore.llm.types import Completion, ImagePart, Message, Role, ToolCall
from chatcore.platform import Capabilities, PlatformError
from chatcore.store.base import SearchHit, StoredMessage, Stores
from chatcore.testing import FakePlatform, ScriptedProvider, memory_stores
from chatcore.testing import message as make_message
from chatcore.tools.registry import ToolContext, TurnState
from chatcore_search import SearchPlugin, SearchSettings


class MemorySearch:
    supports_knn = True

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.chats: dict[str, str] = {}
        self.writes: list[tuple[str, dict, ChatRef]] = []
        self.last_chat: ChatRef | None = None
        self.last_filters = None

    async def index(self, index, doc_id, doc: Mapping, *, chat):
        self.docs[doc_id] = dict(doc)
        self.chats[doc_id] = chat.key
        self.writes.append((doc_id, dict(doc), chat))

    async def search(self, index, *, chat, text=None, vector=None, filters=None, size=10):
        self.last_chat = chat
        self.last_filters = filters
        results = []
        for doc_id, doc in self.docs.items():
            if self.chats[doc_id] != chat.key:
                continue
            if filters and filters.get("kind") == {"not": "bot_answer"}:
                if doc.get("kind") == "bot_answer":
                    continue
            if filters and filters.get("user_keys"):
                values = filters["user_keys"]
                values = [values] if isinstance(values, str) else values
                if not set(values).intersection(doc.get("user_keys", [])):
                    continue
            if text and text.casefold() not in doc.get("text", "").casefold():
                continue
            results.append(SearchHit(doc_id, 1, {**doc, "chat_key": chat.key}))
        return results[:size]

    async def delete(self, index, *, chat, query=None):
        remove = [doc_id for doc_id in self.docs if self.chats[doc_id] == chat.key]
        if query and "term" in query:
            field, value = next(iter(query["term"].items()))
            remove = [doc_id for doc_id in remove if value in self.docs[doc_id].get(field, [])]
        for doc_id in remove:
            del self.docs[doc_id]
            del self.chats[doc_id]
        return len(remove)


class Repo:
    def __init__(self) -> None:
        self.recent: dict[tuple[str, str | None], list[StoredMessage]] = {}
        self.messages: dict[tuple[str, str], StoredMessage] = {}
        self.members: dict[str, list[User]] = {}
        self.recent_calls: list[tuple[ChatRef, int, str | None]] = []

    async def get_message(self, chat, message_id):
        return self.messages.get((chat.key, message_id))

    async def recent_messages(self, chat, *, limit, thread_id=None):
        self.recent_calls.append((chat, limit, thread_id))
        return self.recent.get((chat.key, thread_id), [])[:limit]

    async def find_members(self, chat):
        return self.members.get(chat.key, [])


class Dummy:
    async def fetch(self, sql, *args):
        self.call = (sql, args)
        return []


class RecordingEmbedder:
    dims = 768

    def __init__(self) -> None:
        self.calls = []
        self.inner = FakeEmbedder()

    async def embed_documents(self, texts):
        self.calls.append(list(texts))
        return await self.inner.embed_documents(texts)

    async def embed_query(self, text):
        return await self.inner.embed_query(text)


class DownloadPlatform:
    capabilities = Capabilities(False, False, False, True, True, 4096)

    def __init__(self) -> None:
        self.downloaded = []

    async def download(self, attachment, *, chat, max_bytes):
        self.downloaded.append((attachment, chat, max_bytes))
        return attachment.filename.encode()


def incoming(chat: ChatRef, message_id: str, text: str) -> IncomingMessage:
    user = User(UserRef(chat.platform, "1"), "Alice")
    return IncomingMessage(chat, message_id, user, text, False, sent_at=datetime.now(UTC))


def context(chat: ChatRef, search: MemorySearch | None, *, repo=None, is_dm=False) -> ToolContext:
    user = User(UserRef(chat.platform, "1"), "Alice")
    return ToolContext(
        chat,
        user,
        is_dm,
        platform=DownloadPlatform(),
        stores=Stores(
            repo=repo or Repo(),
            cache=None,
            search=search,  # type: ignore[arg-type]
        ),
    )


async def drain(plugin: SearchPlugin) -> None:
    while plugin._index_tasks:
        await asyncio.gather(*list(plugin._index_tasks))


async def test_eleven_messages_make_two_overlapping_full_windows() -> None:
    search, embedder = MemorySearch(), RecordingEmbedder()
    plugin = SearchPlugin(embedder, settings=SearchSettings(window_size=6))
    chat = ChatRef("tg", "a")
    stores = Stores(repo=Repo(), cache=None, search=search)  # type: ignore[arg-type]

    for number in range(1, 12):
        await plugin.on_message(incoming(chat, str(number), f"message {number}"), stores)
    await drain(plugin)

    assert len([doc for doc in search.docs.values() if doc["kind"] == "message_window"]) == 2
    assert search.docs["window:tg:a:-:1"]["message_ids"] == ["1", "2", "3", "4", "5", "6"]
    assert search.docs["window:tg:a:-:6"]["message_ids"] == ["6", "7", "8", "9", "10", "11"]
    assert len(embedder.calls) == 2


async def test_partial_flush_reuses_id_and_skips_unchanged_buffer() -> None:
    search = MemorySearch()
    plugin = SearchPlugin(FakeEmbedder(), settings=SearchSettings(window_size=4))
    chat = ChatRef("tg", "a")
    stores = Stores(repo=Repo(), cache=None, search=search)  # type: ignore[arg-type]
    for number in range(1, 3):
        await plugin.on_message(incoming(chat, str(number), f"message {number}"), stores)

    await plugin._flush_pending()
    await drain(plugin)
    doc_id = "window:tg:a:-:1"
    assert search.docs[doc_id]["message_ids"] == ["1", "2"]
    first_write_count = len(search.writes)

    await plugin._flush_pending()
    await drain(plugin)
    assert len(search.writes) == first_write_count

    await plugin.on_message(incoming(chat, "3", "message 3"), stores)
    await plugin.on_message(incoming(chat, "4", "message 4"), stores)
    await drain(plugin)
    assert search.docs[doc_id]["message_ids"] == ["1", "2", "3", "4"]


async def test_pending_window_keys_are_bounded() -> None:
    plugin = SearchPlugin(FakeEmbedder(), settings=SearchSettings(window_size=20))
    plugin.max_pending_keys = 2
    stores = Stores(repo=Repo(), cache=None, search=None)  # type: ignore[arg-type]
    for number in range(3):
        await plugin.on_message(incoming(ChatRef("tg", str(number)), str(number), "hello"), stores)

    assert len(plugin._pending) == 2
    assert all(key[0].id != "0" for key in plugin._pending)


async def test_dm_search_only_uses_member_groups_and_is_chat_scoped() -> None:
    search = MemorySearch()
    group = ChatRef("tg", "allowed")
    outsider = ChatRef("tg", "outsider")
    user_ref = UserRef("tg", "1")

    async def groups(user):
        assert user == user_ref
        return [group]

    plugin = SearchPlugin(FakeEmbedder(), member_groups=groups)
    await search.index("chunks", "hit", {"text": "hotel", "message_ids": []}, chat=group)
    await search.index("chunks", "private", {"text": "hotel", "message_ids": []}, chat=outsider)

    result = await plugin.read_chat_history(
        context(ChatRef("tg", "1"), search, is_dm=True), "hotel"
    )
    assert "hotel" in result.content
    assert search.last_chat == group


async def test_bot_replies_are_indexed_and_excluded_unless_requested() -> None:
    search = MemorySearch()
    repo = Repo()
    chat = ChatRef("tg", "a")
    bot = StoredMessage(
        chat,
        "bot-1",
        UserRef("tg", "bot"),
        "Bot",
        "The booking is confirmed.",
        datetime.now(UTC),
        is_bot=True,
    )
    repo.recent[(chat.key, None)] = [bot]
    plugin = SearchPlugin(FakeEmbedder(), settings=SearchSettings(window_size=2))
    stores = Stores(repo=repo, cache=None, search=search)  # type: ignore[arg-type]
    await plugin.on_message(incoming(chat, "1", "hello"), stores)
    await plugin.on_message(incoming(chat, "2", "booking"), stores)
    await drain(plugin)

    assert search.docs["bot:tg:a:bot-1"]["kind"] == "bot_answer"
    ctx = context(chat, search, repo=repo)
    excluded = await plugin.read_chat_history(ctx, "booking")
    included = await plugin.read_chat_history(ctx, "booking", include_bot_answers=True)
    assert "confirmed" not in excluded.content
    assert "confirmed" in included.content


async def test_search_falls_back_to_escaped_ilike_and_parses_since() -> None:
    plugin = SearchPlugin(FakeEmbedder())
    db, repo = Dummy(), Repo()
    ctx = context(ChatRef("tg", "a"), None, repo=repo)
    ctx.stores.db = db

    await plugin._search_chat(ctx, ctx.chat, "100%_\\", None, datetime(2026, 10, 1, tzinfo=UTC), 5)

    assert db.call[0].count("WHERE") == 1
    assert "text ILIKE $2 ESCAPE" in db.call[0]
    assert db.call[1][1] == r"%100\%\_\\%"
    assert db.call[1][4] == datetime(2026, 10, 1, tzinfo=UTC)


async def test_bad_since_is_rejected_before_search() -> None:
    plugin = SearchPlugin(FakeEmbedder())
    result = await plugin.read_chat_history(
        context(ChatRef("tg", "a"), MemorySearch()), "x", since="tomorrow"
    )

    assert result.is_error
    assert result.content == "since must be an ISO date like 2026-10-01"


async def test_since_date_is_utc_midnight_in_search_filters() -> None:
    search, repo = MemorySearch(), Repo()
    plugin = SearchPlugin(FakeEmbedder())

    await plugin.read_chat_history(
        context(ChatRef("tg", "a"), search, repo=repo), "hotel", since="2026-10-01"
    )

    assert search.last_filters["ts_end"]["gte"] == "2026-10-01T00:00:00+00:00"


async def test_from_user_resolves_display_name_and_unknown_name_errors() -> None:
    search, repo = MemorySearch(), Repo()
    chat = ChatRef("tg", "a")
    repo.members[chat.key] = [User(UserRef("tg", "42"), "Alice", username="alice_42")]
    await search.index(
        "chunks",
        "alice-doc",
        {"text": "hotel", "message_ids": [], "user_keys": ["tg:42"]},
        chat=chat,
    )
    plugin = SearchPlugin(FakeEmbedder())
    ctx = context(chat, search, repo=repo)

    results = [
        await plugin.read_chat_history(ctx, "hotel", from_user=alias)
        for alias in ("Alice", "TG:42", "42", "alice_42", "@ALICE_42")
    ]
    missing = await plugin.read_chat_history(ctx, "hotel", from_user="No One")

    assert all('"text":"hotel"' in result.content for result in results)
    assert all('"display_name":"unknown speaker"' in result.content for result in results)
    assert all(result.untrusted for result in results)
    assert all(result.persist_content is False for result in results)
    assert search.last_filters["user_keys"] == ["tg:42"]
    assert missing.is_error
    assert missing.content == "No member matches from_user"


async def test_hydrated_history_preserves_historical_speakers_replies_and_mentions():
    search, repo = MemorySearch(), Repo()
    chat = ChatRef("tg", "history")
    alice_key, bob_key = UserRef("tg", "8"), UserRef("tg", "9")
    now = datetime.now(UTC)
    first = StoredMessage(
        chat,
        "m1",
        alice_key,
        "Alex",
        "Alex calls Bob Captain",
        now,
        username="alice_old",
        mentions=(Mention(0, 4, "text_mention", "Alex", bob_key, "bob_old", "Alex"),),
    )
    second = StoredMessage(
        chat,
        "m2",
        bob_key,
        "Alex",
        "Alex agrees",
        now,
        reply_to_id="m1",
        username="bob_old",
        mentions=(Mention(0, 4, "text_mention", "Alex", alice_key, "alice_old", "Alex"),),
    )
    repo.messages[(chat.key, "m1")] = first
    repo.messages[(chat.key, "m2")] = second
    repo.members[chat.key] = [
        User(alice_key, "Alex", username="alice_new"),
        User(bob_key, "Alex", username="bob_new"),
    ]
    await search.index(
        "chunks",
        "history-hit",
        {"text": "Captain", "message_ids": ["m1", "m2"], "user_keys": [alice_key.key]},
        chat=chat,
    )
    plugin = SearchPlugin(FakeEmbedder())

    result = await plugin.read_chat_history(context(chat, search, repo=repo), "Captain")

    assert result.untrusted
    assert '"user_key":"tg:8"' in result.content
    assert '"user_key":"tg:9"' in result.content
    assert '"username":"alice_old"' in result.content
    assert '"username":"bob_old"' in result.content
    assert '"reply_to_id":"m1"' in result.content
    assert '"target_user_key":"tg:9"' in result.content
    assert "alice_new" not in result.content
    assert "bob_new" not in result.content


async def test_history_tool_output_is_live_only_for_group_and_authorized_dm_reads():
    alice = User(UserRef("fake", "alice"), "Alice", username="alice_old")
    bob = User(UserRef("fake", "bob"), "Bob", username="bob")
    source_chat = ChatRef("fake", "history-group")
    source_text = "I call Bob Captain"
    mention_start = source_text.index("Bob")
    source_message = replace(
        make_message(source_text, sender=alice, chat=source_chat),
        mentions=(
            Mention(
                mention_start,
                mention_start + len("Bob"),
                "text_mention",
                "Bob",
                bob.ref,
                bob.username,
                bob.display_name,
            ),
        ),
    )

    async def run_read(*, in_dm: bool):
        stores = memory_stores()
        search = MemorySearch()
        stores.search = search  # type: ignore[assignment]
        await stores.repo.log_message(source_message)
        await stores.repo.upsert_member(source_chat, alice)
        await stores.repo.upsert_member(source_chat, bob)
        await search.index(
            "chunks",
            "alice-history",
            {
                "text": "I call Bob Captain",
                "message_ids": [source_message.message_id],
                "user_keys": [alice.ref.key],
                "kind": "message",
            },
            chat=source_chat,
        )

        async def member_groups(user_ref):
            assert user_ref == bob.ref
            return [source_chat]

        search_plugin = SearchPlugin(FakeEmbedder(), member_groups=member_groups)
        seen_results = []
        read_history = search_plugin.read_chat_history

        async def tracked_read_history(ctx, **kwargs):
            result = await read_history(ctx, **kwargs)
            seen_results.append(result)
            return result

        search_plugin.read_chat_history = tracked_read_history
        conversation = ConversationPlugin()
        provider = ScriptedProvider(
            [
                Completion(
                    Message.assistant(
                        "",
                        (ToolCall("history-call", "read_chat_history", {"query": "Captain"}),),
                    ),
                    "tool_use",
                ),
                Completion(Message.assistant("The note says Bob is Captain."), "end_turn"),
            ]
        )
        responder = AgentResponder(
            provider,
            system_prompt="",
            settings=AgentSettings(),
            limits=Limits(stores.cache),
        )
        app = BotApp(
            FakePlatform(),
            stores=stores,
            access=AccessPolicy([source_chat.key], [], owners=[bob.ref.key]),
            plugins=[search_plugin, conversation],
            responder=responder,
            bot_name="bot",
            group_mentions_only=True,
        )
        request = make_message(
            "Look up Captain",
            sender=bob,
            chat=source_chat,
            dm=in_dm,
            mention=not in_dm,
        )

        await app.handle(request)

        assert len(seen_results) == 1
        live_result = seen_results[0]
        assert live_result.untrusted and live_result.persist_content is False
        assert '"user_key":"fake:alice"' in live_result.content
        assert '"username":"alice_old"' in live_result.content
        assert '"target_user_key":"fake:bob"' in live_result.content
        live_tool_message = next(
            item for item in provider.calls[1]["messages"] if item.role is Role.TOOL
        )
        assert '"user_key":"fake:alice"' in live_tool_message.text
        assert '"target_user_key":"fake:bob"' in live_tool_message.text

        saved_turns = await conversation.store.recent(request.chat, None)
        tool_turn = next(turn for turn in saved_turns if turn.role == "tool")
        assert tool_turn.content["tool_call_id"] == "history-call"
        assert tool_turn.content["is_error"] is False
        assert tool_turn.content["untrusted"] is True
        assert tool_turn.content["text"] == "[Tool result omitted from persistent history.]"
        assert "fake:alice" not in tool_turn.content["text"]
        assert "alice_old" not in tool_turn.content["text"]
        assert next(turn.content["text"] for turn in saved_turns if turn.role == "user") == (
            "Look up Captain"
        )
        assistant_texts = [turn.content["text"] for turn in saved_turns if turn.role == "assistant"]
        assert assistant_texts[-1] == "The note says Bob is Captain."
        assert (await stores.repo.get_message(source_chat, source_message.message_id)).username == (
            "alice_old"
        )

        await search_plugin.purge_user(source_chat, alice.ref, stores)
        await conversation.purge_user(source_chat, alice.ref, stores)
        await stores.repo.purge_user(source_chat, alice.ref)
        remaining_turns = await conversation.store.recent(request.chat, None)
        remaining_tool = next(turn for turn in remaining_turns if turn.role == "tool")
        assert remaining_tool.content["text"] == "[Tool result omitted from persistent history.]"
        assert "fake:alice" not in remaining_tool.content["text"]
        assert await stores.repo.get_message(source_chat, source_message.message_id) is None

    await run_read(in_dm=False)
    await run_read(in_dm=True)


async def test_forgetme_clears_pending_mention_metadata_only_in_target_chat():
    plugin = SearchPlugin(FakeEmbedder())
    chat, other = ChatRef("tg", "pending"), ChatRef("tg", "other")
    alice = User(UserRef("tg", "8"), "Alice", username="alice_old")
    bob = User(UserRef("tg", "9"), "Bob")
    mentioned = IncomingMessage(
        chat,
        "bob-message",
        bob,
        "Alice was named",
        False,
        mentions=(Mention(0, 5, "text_mention", "Alice", alice.ref, "alice_old", "Alice"),),
    )
    alice_message = IncomingMessage(chat, "alice-message", alice, "own message", False)
    other_copy = IncomingMessage(
        other,
        "other-message",
        bob,
        "Alice elsewhere",
        False,
        mentions=(Mention(0, 5, "text_mention", "Alice", alice.ref, "alice_old", "Alice"),),
    )
    stores = context(chat, None).stores
    for incoming in (mentioned, alice_message, other_copy):
        await plugin.on_message(incoming, stores)
    await asyncio.gather(*plugin._index_tasks)

    await plugin.purge_user(chat, alice.ref, stores)

    assert [item.message_id for item in plugin._pending[(chat, None)].messages] == ["bob-message"]
    assert plugin._pending[(chat, None)].messages[0].mentions == ()
    assert plugin._pending[(other, None)].messages[0].mentions == other_copy.mentions


async def test_attachment_defaults_to_current_user_not_newer_other_user() -> None:
    chat = ChatRef("tg", "a")
    user = UserRef("tg", "1")
    from chatcore.events import Attachment, AttachmentKind

    own_attachment = Attachment("mine", AttachmentKind.DOCUMENT, "own.txt")
    other_attachment = Attachment("other", AttachmentKind.DOCUMENT, "other.txt")

    class AttachmentRepo(Repo):
        async def recent_messages(self, chat, *, limit, thread_id=None):
            older = datetime(2026, 10, 1, tzinfo=UTC)
            return [
                StoredMessage(
                    chat,
                    "own-msg",
                    user,
                    "Alice",
                    "",
                    older,
                    attachments=(own_attachment,),
                ),
                StoredMessage(
                    chat,
                    "other-msg",
                    UserRef("tg", "2"),
                    "Bob",
                    "",
                    datetime(2026, 10, 2, tzinfo=UTC),
                    attachments=(other_attachment,),
                ),
            ][:limit]

    plugin = SearchPlugin(FakeEmbedder())
    ctx = context(chat, MemorySearch(), repo=AttachmentRepo())
    result = await plugin.read_attachment(ctx)

    assert result.content == "own.txt"
    assert ctx.platform.downloaded[0][0] == own_attachment


async def test_attachment_prefers_own_file_even_when_message_is_reply() -> None:
    from chatcore.events import Attachment, AttachmentKind

    chat = ChatRef("tg", "a")
    user = UserRef("tg", "1")
    own_attachment = Attachment("mine", AttachmentKind.DOCUMENT, "own.txt")
    reply_attachment = Attachment("reply", AttachmentKind.DOCUMENT, "reply.txt")

    class AttachmentRepo(Repo):
        def __init__(self):
            super().__init__()
            self.lookups = []

        async def recent_messages(self, chat, *, limit, thread_id=None):
            return [
                StoredMessage(
                    chat,
                    "own-msg",
                    user,
                    "Alice",
                    "",
                    datetime.now(UTC),
                    attachments=(own_attachment,),
                    reply_to_id="reply-msg",
                )
            ]

        async def get_message(self, chat, message_id):
            self.lookups.append(message_id)
            return StoredMessage(
                chat,
                message_id,
                user,
                "Alice",
                "",
                datetime.now(UTC),
                attachments=(reply_attachment,),
            )

    plugin = SearchPlugin(FakeEmbedder())
    repo = AttachmentRepo()
    ctx = context(chat, MemorySearch(), repo=repo)
    result = await plugin.read_attachment(ctx)

    assert result.content == "own.txt"
    assert repo.lookups == []


async def test_attachment_chunks_use_sender_and_background_indexing() -> None:
    from chatcore.events import Attachment, AttachmentKind

    chat = ChatRef("tg", "a")
    sender = UserRef("tg", "sender")
    attachment = Attachment("file", AttachmentKind.DOCUMENT, "words.txt")
    stored = StoredMessage(
        chat, "msg-1", sender, "Sender", "", datetime.now(UTC), attachments=(attachment,)
    )

    class AttachmentRepo(Repo):
        async def get_message(self, chat, message_id):
            return stored

    search, embedder = MemorySearch(), RecordingEmbedder()
    plugin = SearchPlugin(embedder)
    ctx = context(chat, search, repo=AttachmentRepo())

    class TextPlatform(DownloadPlatform):
        async def download(self, attachment, *, chat, max_bytes):
            return ("word " * 1000).encode()

    ctx.platform = TextPlatform()
    result = await plugin.read_attachment(ctx, "msg-1")

    assert result.untrusted
    assert not search.docs
    await drain(plugin)
    attachment_docs = {
        doc_id: doc for doc_id, doc in search.docs.items() if doc["kind"] == "attachment"
    }
    assert list(attachment_docs) == [
        f"attachment:{chat.key}:msg-1:file:{number}" for number in range(4)
    ]
    assert all(doc["user_keys"] == [sender.key] for doc in attachment_docs.values())
    assert len(embedder.calls) == 1
    chunks = list(attachment_docs.values())
    assert [len(doc["text"]) for doc in chunks] == [1600, 1600, 1600, 800]
    assert chunks[0]["text"][-200:] == chunks[1]["text"][:200]


async def test_oversized_known_attachment_is_rejected_before_download() -> None:
    from chatcore.events import Attachment, AttachmentKind
    from chatcore_search import SearchSettings

    chat = ChatRef("tg", "a")
    attachment = Attachment("huge", AttachmentKind.DOCUMENT, "huge.txt", size_bytes=5)
    stored = StoredMessage(
        chat, "msg", UserRef("tg", "1"), "Alice", "", datetime.now(UTC), attachments=(attachment,)
    )

    class AttachmentRepo(Repo):
        async def get_message(self, chat, message_id):
            return stored

    plugin = SearchPlugin(FakeEmbedder(), settings=SearchSettings(attachment_max_bytes=4))
    ctx = context(chat, MemorySearch(), repo=AttachmentRepo())
    result = await plugin.read_attachment(ctx, "msg")

    assert result.is_error
    assert not ctx.platform.downloaded


async def test_image_attachment_returns_image_for_vision_turn_and_decrements_limit() -> None:
    from chatcore.events import Attachment, AttachmentKind

    chat = ChatRef("tg", "a")
    attachment = Attachment("photo", AttachmentKind.PHOTO)
    stored = StoredMessage(
        chat, "msg", UserRef("tg", "1"), "Alice", "", datetime.now(UTC), attachments=(attachment,)
    )

    class AttachmentRepo(Repo):
        async def get_message(self, chat, message_id):
            return stored

    ctx = context(chat, MemorySearch(), repo=AttachmentRepo())
    ctx.turn = TurnState(vision=True, images_remaining=1)

    class ImagePlatform(DownloadPlatform):
        async def download(self, attachment, *, chat, max_bytes):
            self.downloaded.append((attachment, chat, max_bytes))
            return b"\xff\xd8\xffjpeg"

    ctx.platform = ImagePlatform()
    result = await SearchPlugin(FakeEmbedder()).read_attachment(ctx, "msg")

    assert not result.is_error
    assert result.untrusted
    assert len(result.images) == 1 and isinstance(result.images[0], ImagePart)
    assert ctx.turn.images_remaining == 0
    assert not ctx.stores.search.docs


async def test_image_attachment_requires_vision_and_enforces_turn_limit() -> None:
    from chatcore.events import Attachment, AttachmentKind

    chat = ChatRef("tg", "a")
    attachment = Attachment("photo", AttachmentKind.PHOTO)
    stored = StoredMessage(
        chat, "msg", UserRef("tg", "1"), "Alice", "", datetime.now(UTC), attachments=(attachment,)
    )

    class AttachmentRepo(Repo):
        async def get_message(self, chat, message_id):
            return stored

    plugin = SearchPlugin(FakeEmbedder())
    ctx = context(chat, MemorySearch(), repo=AttachmentRepo())
    result = await plugin.read_attachment(ctx, "msg")
    assert result.is_error
    assert result.content == "attachment unavailable: image viewing is not enabled"
    assert not ctx.platform.downloaded

    ctx.turn = TurnState(vision=True, images_remaining=0)
    result = await plugin.read_attachment(ctx, "msg")
    assert result.is_error
    assert result.content == "attachment unavailable: image limit for this turn reached"


async def test_image_attachment_rejects_unsupported_bytes_and_cross_topic() -> None:
    from chatcore.events import Attachment, AttachmentKind

    chat = ChatRef("tg", "a")
    attachment = Attachment("photo", AttachmentKind.PHOTO)
    stored = StoredMessage(
        chat,
        "msg",
        UserRef("tg", "1"),
        "Alice",
        "",
        datetime.now(UTC),
        thread_id="other-topic",
        attachments=(attachment,),
    )

    class AttachmentRepo(Repo):
        async def get_message(self, chat, message_id):
            return stored

    ctx = context(chat, MemorySearch(), repo=AttachmentRepo())
    ctx.thread_id = "topic"
    ctx.turn = TurnState(vision=True, images_remaining=1)
    result = await SearchPlugin(FakeEmbedder()).read_attachment(ctx, "msg")
    assert result.content == "attachment unavailable: message is in a different topic"

    stored = replace(stored, thread_id="topic")
    ctx.platform = DownloadPlatform()
    ctx.platform.download = lambda *args, **kwargs: _bad_image_bytes()
    result = await SearchPlugin(FakeEmbedder()).read_attachment(ctx, "msg")
    assert result.content == "attachment unavailable: not JPEG/PNG/WebP"


async def _bad_image_bytes():
    return b"GIF89a not supported"


async def test_attachment_image_download_rejects_undecodable_bytes():
    from chatcore.events import Attachment, AttachmentKind

    chat = ChatRef("tg", "a")
    stored = StoredMessage(
        chat,
        "msg",
        UserRef("tg", "1"),
        "Alice",
        "",
        datetime.now(UTC),
        attachments=(Attachment("photo", AttachmentKind.PHOTO),),
    )

    class AttachmentRepo(Repo):
        async def get_message(self, chat, message_id):
            return stored

    class UndecodablePlatform(DownloadPlatform):
        async def download(self, attachment, *, chat, max_bytes):
            return b"not an image"

    ctx = context(chat, MemorySearch(), repo=AttachmentRepo())
    ctx.platform = UndecodablePlatform()
    ctx.turn = TurnState(vision=True, images_remaining=1)
    result = await SearchPlugin(FakeEmbedder()).read_attachment(ctx, "msg")

    assert result.content.startswith("attachment unavailable:")
    assert result.content.endswith("not JPEG/PNG/WebP")
    assert ctx.turn.images_remaining == 1


async def test_read_attachment_reuses_turn_image_download_and_reports_platform_failure():
    from chatcore.events import Attachment, AttachmentKind

    chat = ChatRef("tg", "a")
    attachment = Attachment("shared-photo", AttachmentKind.PHOTO)
    stored = StoredMessage(
        chat,
        "msg",
        UserRef("tg", "1"),
        "Alice",
        "",
        datetime.now(UTC),
        attachments=(attachment,),
    )

    class AttachmentRepo(Repo):
        async def get_message(self, chat, message_id):
            return stored

    ctx = context(chat, MemorySearch(), repo=AttachmentRepo())
    ctx.turn = TurnState(
        vision=True,
        images_remaining=1,
        downloads={attachment.id: b"\xff\xd8\xffcached"},
    )
    result = await SearchPlugin(FakeEmbedder()).read_attachment(ctx, "msg")
    assert result.images[0].data == b"\xff\xd8\xffcached"
    assert ctx.platform.downloaded == []

    class FailedPlatform(DownloadPlatform):
        async def download(self, attachment, *, chat, max_bytes):
            raise PlatformError("TimedOut")

    ctx.platform = FailedPlatform()
    ctx.turn.downloads.clear()
    ctx.turn.images_remaining = 1
    result = await SearchPlugin(FakeEmbedder()).read_attachment(ctx, "msg")
    assert result.content == "attachment unavailable: download failed, please try again"


async def test_empty_attachment_text_is_an_error_and_is_not_indexed():
    from io import BytesIO

    from chatcore.events import Attachment, AttachmentKind
    from pypdf import PdfWriter

    chat = ChatRef("tg", "a")
    attachment = Attachment("file", AttachmentKind.DOCUMENT, "empty.txt")
    stored = StoredMessage(
        chat, "msg", UserRef("tg", "1"), "Alice", "", datetime.now(UTC), attachments=(attachment,)
    )

    class AttachmentRepo(Repo):
        async def get_message(self, chat, message_id):
            return stored

    class EmptyTextPlatform(DownloadPlatform):
        async def download(self, attachment, *, chat, max_bytes):
            return b" \n\t"

    search = MemorySearch()
    ctx = context(chat, search, repo=AttachmentRepo())
    ctx.platform = EmptyTextPlatform()
    result = await SearchPlugin(FakeEmbedder()).read_attachment(ctx, "msg")

    assert result.content == "attachment unavailable: no extractable text"
    assert result.is_error
    assert not search.docs

    pdf_buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(pdf_buffer)
    pdf_attachment = Attachment("pdf", AttachmentKind.DOCUMENT, "scan.pdf")
    pdf_stored = replace(stored, attachments=(pdf_attachment,))

    class PdfRepo(AttachmentRepo):
        async def get_message(self, chat, message_id):
            return pdf_stored

    class PdfPlatform(DownloadPlatform):
        async def download(self, attachment, *, chat, max_bytes):
            return pdf_buffer.getvalue()

    ctx.stores.repo = PdfRepo()
    ctx.platform = PdfPlatform()
    result = await SearchPlugin(FakeEmbedder()).read_attachment(ctx, "msg")

    assert result.content == "attachment unavailable: no extractable text"
    assert result.is_error
    assert not search.docs


async def test_attachment_errors_scope_topic_chat_and_index():
    from chatcore.events import Attachment, AttachmentKind

    chat = ChatRef("tg", "a")
    other_chat = ChatRef("tg", "b")
    attachment = Attachment("file", AttachmentKind.DOCUMENT, "words.txt")
    stored = StoredMessage(
        chat,
        "msg",
        UserRef("tg", "1"),
        "Alice",
        "",
        datetime.now(UTC),
        thread_id="topic",
        attachments=(attachment,),
    )

    class AttachmentRepo(Repo):
        async def get_message(self, requested_chat, message_id):
            return stored if requested_chat == chat and message_id == "msg" else None

    plugin = SearchPlugin(FakeEmbedder())
    ctx = context(chat, MemorySearch(), repo=AttachmentRepo())
    ctx.thread_id = "topic"
    ctx.platform.download = lambda *args, **kwargs: _blank_text()
    missing = await plugin.read_attachment(ctx, "missing")
    assert missing.content == "attachment unavailable: message not found in this chat"
    no_attachment = replace(stored, attachments=())

    class EmptyRepo(AttachmentRepo):
        async def get_message(self, requested_chat, message_id):
            return no_attachment if requested_chat == chat else None

    ctx.stores.repo = EmptyRepo()
    absent = await plugin.read_attachment(ctx, "msg", index=0)
    assert absent.content == "attachment unavailable: message has no attachment at index 0"
    ctx.chat = other_chat
    isolated = await plugin.read_attachment(ctx, "msg")
    assert isolated.content == "attachment unavailable: message not found in this chat"


async def _blank_text():
    return b""


async def test_oversized_image_attachment_returns_size_error_before_image_hint() -> None:
    from chatcore.events import Attachment, AttachmentKind

    chat = ChatRef("tg", "a")
    attachment = Attachment("photo", AttachmentKind.PHOTO, size_bytes=5)
    stored = StoredMessage(
        chat, "msg", UserRef("tg", "1"), "Alice", "", datetime.now(UTC), attachments=(attachment,)
    )

    class AttachmentRepo(Repo):
        async def get_message(self, chat, message_id):
            return stored

    ctx = context(
        chat,
        MemorySearch(),
        repo=AttachmentRepo(),
    )
    plugin = SearchPlugin(FakeEmbedder(), settings=SearchSettings(attachment_max_bytes=4))
    result = await plugin.read_attachment(ctx, "msg")

    assert result.is_error
    assert result.content == "attachment unavailable: exceeds the size limit"
    assert not ctx.platform.downloaded


async def test_unsupported_attachment_returns_error_without_indexing() -> None:
    from chatcore.events import Attachment, AttachmentKind

    chat = ChatRef("tg", "a")
    attachment = Attachment("archive", AttachmentKind.DOCUMENT, "archive.zip")
    stored = StoredMessage(
        chat, "msg", UserRef("tg", "1"), "Alice", "", datetime.now(UTC), attachments=(attachment,)
    )

    class AttachmentRepo(Repo):
        async def get_message(self, chat, message_id):
            return stored

    search = MemorySearch()
    plugin = SearchPlugin(FakeEmbedder())
    ctx = context(chat, search, repo=AttachmentRepo())
    result = await plugin.read_attachment(ctx, "msg")

    assert result.is_error
    assert result.content == "attachment unavailable: unsupported type"
    assert not search.docs


async def test_history_expansion_uses_searched_chat_not_index_chat_key() -> None:
    class EvilSearch(MemorySearch):
        async def search(self, index, *, chat, **kwargs):
            return [SearchHit("x", 1, {"chat_key": "tg:wrong", "message_ids": ["m"], "text": "ok"})]

    class TrackingRepo(Repo):
        def __init__(self):
            super().__init__()
            self.lookups = []

        async def get_message(self, chat, message_id):
            self.lookups.append((chat, message_id))
            return None

    chat = ChatRef("tg", "actual")
    repo = TrackingRepo()
    plugin = SearchPlugin(FakeEmbedder())

    await plugin.read_chat_history(context(chat, EvilSearch(), repo=repo), "ok")

    assert repo.lookups == [(chat, "m")]


async def test_es_failure_falls_back_to_db_and_search_command_is_admin_only() -> None:
    class BrokenSearch(MemorySearch):
        async def search(self, *args, **kwargs):
            raise ConnectionError("private connection details")

    db = Dummy()
    stores = Stores(repo=Repo(), cache=None, db=db, search=BrokenSearch())  # type: ignore[arg-type]
    ctx = context(ChatRef("tg", "a"), BrokenSearch())
    ctx.stores = stores
    plugin = SearchPlugin(FakeEmbedder())

    await plugin._search_chat(ctx, ctx.chat, "hotel", None, None, 5)

    assert "chat_key=$1 AND text ILIKE $2" in db.call[0]
    assert db.call[1][:3] == ("tg:a", "%hotel%", 5)
    assert plugin.commands()[0].admin_only is True


async def test_purge_user_and_chat_are_scoped() -> None:
    search = MemorySearch()
    plugin = SearchPlugin(FakeEmbedder())
    chat_a, chat_b = ChatRef("tg", "a"), ChatRef("tg", "b")
    await search.index("chunks", "a1", {"user_keys": ["tg:1"]}, chat=chat_a)
    await search.index("chunks", "a2", {"user_keys": ["tg:2"]}, chat=chat_a)
    await search.index("chunks", "b1", {"user_keys": ["tg:1"]}, chat=chat_b)
    stores = Stores(repo=Repo(), cache=None, search=search)  # type: ignore[arg-type]

    await plugin.purge_user(chat_a, UserRef("tg", "1"), stores)
    assert set(search.docs) == {"a2", "b1"}
    await plugin.purge_chat(chat_a, stores)
    assert set(search.docs) == {"b1"}


async def test_purge_user_trims_pending_window_before_es_delete_and_flush() -> None:
    search, repo = MemorySearch(), Repo()
    chat = ChatRef("tg", "purge-user")
    user_a, user_b = UserRef("tg", "1"), UserRef("tg", "2")
    plugin = SearchPlugin(FakeEmbedder(), settings=SearchSettings(window_size=4))
    stores = Stores(repo=repo, cache=None, search=search)  # type: ignore[arg-type]
    messages = [
        IncomingMessage(
            chat, "a", User(user_a, "Alice"), "private marker", False, sent_at=datetime.now(UTC)
        ),
        IncomingMessage(
            chat, "b", User(user_b, "Bob"), "public marker", False, sent_at=datetime.now(UTC)
        ),
    ]
    for message in messages:
        await plugin.on_message(message, stores)
    await plugin.purge_user(chat, user_a, stores)
    await plugin._flush_pending()
    await drain(plugin)

    assert all("private marker" not in doc["text"] for doc in search.docs.values())
    assert all(user_a.key not in doc["user_keys"] for doc in search.docs.values())
    assert any("public marker" in doc["text"] for doc in search.docs.values())


async def test_purge_user_resets_indexed_partial_window_for_rewrite() -> None:
    search = MemorySearch()
    chat, user = ChatRef("tg", "purge-indexed"), UserRef("tg", "1")
    user_b = UserRef("tg", "2")
    plugin = SearchPlugin(FakeEmbedder(), settings=SearchSettings(window_size=4))
    stores = Stores(repo=Repo(), cache=None, search=search)  # type: ignore[arg-type]
    await plugin.on_message(
        IncomingMessage(chat, "a", User(user, "Alice"), "private marker", False), stores
    )
    await plugin.on_message(
        IncomingMessage(chat, "b", User(user_b, "Bob"), "public marker", False), stores
    )
    await plugin._flush_pending()
    await drain(plugin)
    assert plugin._pending[(chat, None)].partial_indexed

    await plugin.purge_user(chat, user, stores)
    await plugin._flush_pending()
    await drain(plugin)

    assert any("public marker" in doc["text"] for doc in search.docs.values())
    assert all("private marker" not in doc["text"] for doc in search.docs.values())


async def test_purge_user_cleans_buffers_without_search_store() -> None:
    chat, user = ChatRef("tg", "purge-memory"), UserRef("tg", "1")
    plugin = SearchPlugin(FakeEmbedder())
    stores = Stores(repo=Repo(), cache=None, search=None)  # type: ignore[arg-type]
    message = IncomingMessage(
        chat, "a", User(user, "Alice"), "private marker", False, sent_at=datetime.now(UTC)
    )
    await plugin.on_message(message, stores)

    await plugin.purge_user(chat, user, stores)

    assert not plugin._pending


async def test_purge_chat_drops_buffers_and_bot_cache_before_flush() -> None:
    search, repo = MemorySearch(), Repo()
    chat = ChatRef("tg", "purge-chat")
    plugin = SearchPlugin(FakeEmbedder())
    stores = Stores(repo=repo, cache=None, search=search)  # type: ignore[arg-type]
    await plugin.on_message(incoming(chat, "a", "chat marker"), stores)
    await drain(plugin)
    plugin._indexed_bot_ids[chat.key] = OrderedDict([("bot-a", None)])
    plugin._bot_pending[chat.key] = {"bot-b"}

    await plugin.purge_chat(chat, stores)
    await plugin._flush_pending()
    await drain(plugin)

    assert not any(stored_chat == chat.key for stored_chat in search.chats.values())
    assert all(key[0] != chat for key in plugin._pending)
    assert chat.key not in plugin._indexed_bot_ids
    assert chat.key not in plugin._bot_pending


async def test_clean_buffer_is_not_scanned_on_repeated_flush() -> None:
    repo = Repo()
    chat = ChatRef("tg", "dirty-flush")
    plugin = SearchPlugin(FakeEmbedder())
    stores = Stores(repo=repo, cache=None, search=MemorySearch())  # type: ignore[arg-type]
    await plugin.on_message(incoming(chat, "a", "hello"), stores)
    await drain(plugin)
    repo.recent_calls.clear()

    await plugin._flush_pending()
    await drain(plugin)
    first_flush_calls = len(repo.recent_calls)
    await plugin._flush_pending()
    await drain(plugin)

    assert first_flush_calls == 1
    assert len(repo.recent_calls) == first_flush_calls
