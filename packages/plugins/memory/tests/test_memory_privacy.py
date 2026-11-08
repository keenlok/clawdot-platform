import asyncio

import pytest
from chatcore.embed import FakeEmbedder
from chatcore.events import ChatRef, UserRef
from chatcore.pending import PendingAction
from chatcore.plugin import CommandContext
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import FakePlatform, message, user
from chatcore.tools.registry import ToolContext
from chatcore_memory.plugin import MemoryPlugin


class MemorySearchFake:
    def __init__(self):
        self.docs = {}
        self.chats = {}
        self.fail_delete = False

    async def index(self, _index, doc_id, doc, *, chat):
        self.docs[doc_id] = doc
        self.chats[doc_id] = chat.key

    async def delete(self, _index, *, chat, query=None, refresh_first=False):
        if self.fail_delete:
            raise ConnectionError("search unavailable")
        term = (query or {}).get("term", {})
        doomed = [
            doc_id
            for doc_id, doc in self.docs.items()
            if self.chats[doc_id] == chat.key
            and all(doc.get(key) == value for key, value in term.items())
        ]
        for doc_id in doomed:
            self.docs.pop(doc_id)
            self.chats.pop(doc_id)
        return len(doomed)


class ToggleEmbedder:
    def __init__(self):
        self.fail = True

    async def embed_documents(self, texts):
        if self.fail:
            raise ConnectionError("embedding unavailable")
        return [[0.1, 0.2] for _text in texts]

    async def embed_query(self, _text):
        if self.fail:
            raise ConnectionError("embedding unavailable")
        return [0.1, 0.2]


async def test_forgetme_removes_sole_support_but_keeps_independent_shared_fact():
    chat = ChatRef("fake", "memory-privacy")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    alice, bob = user("alice"), user("bob")
    alice_message = message("The group likes tea", sender=alice, chat=chat)
    bob_message = message("The group likes tea", sender=bob, chat=chat)
    await stores.repo.log_message(alice_message)
    await stores.repo.log_message(bob_message)
    plugin = MemoryPlugin()
    first = await plugin._save(
        chat,
        "The group likes tea",
        stores,
        source="auto",
        pinned=False,
        created_by=alice.ref,
        epoch=0,
        source_ids=[(alice_message.message_id, "statement")],
        provenance="known",
    )
    await plugin._save(
        chat,
        "The group likes tea",
        stores,
        source="auto",
        pinned=False,
        created_by=bob.ref,
        epoch=0,
        source_ids=[(bob_message.message_id, "statement")],
        provenance="known",
    )

    await plugin._repo(stores).purge_user(chat, alice.ref)

    remaining = await plugin._repo(stores).list(chat)
    assert [row["id"] for row in remaining] == [first["row"]["id"]]
    assert [
        link["source_author_key"] for link in plugin._repo(stores).sources[first["row"]["id"]]
    ] == [bob.ref.key]


async def test_forgetme_deletes_fact_with_only_request_link():
    chat = ChatRef("fake", "request-only")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    alice = user("alice")
    request = message("Remember this", sender=alice, chat=chat)
    await stores.repo.log_message(request)
    plugin = MemoryPlugin()
    saved = await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="explicit",
        pinned=False,
        created_by=alice.ref,
        epoch=0,
        source_ids=[(request.message_id, "request")],
        provenance="request_only",
    )

    await plugin._repo(stores).purge_user(chat, alice.ref)

    assert await plugin._repo(stores).get(chat, saved["row"]["id"]) is None


async def test_legacy_auto_fact_is_deleted_despite_another_authors_request_link():
    chat = ChatRef("fake", "legacy-request-link")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    alice, bob = user("alice"), user("bob")
    request = message("Please remember this", sender=bob, chat=chat)
    await stores.repo.log_message(request)
    plugin = MemoryPlugin()
    legacy = await plugin._repo(stores).insert(
        chat,
        "The group meets Fridays",
        source="auto",
        pinned=False,
        created_by=alice.ref,
    )
    await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="explicit",
        pinned=False,
        created_by=bob.ref,
        source_ids=[(request.message_id, "request")],
        provenance="request_only",
    )

    await plugin._repo(stores).purge_user(chat, alice.ref)

    assert await plugin._repo(stores).get(chat, legacy["id"]) is None


async def test_legacy_auto_fact_with_another_authors_statement_is_preserved():
    chat = ChatRef("fake", "legacy-other-statement")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    alice, bob = user("alice"), user("bob")
    statement = message("The group meets Fridays", sender=bob, chat=chat)
    await stores.repo.log_message(statement)
    plugin = MemoryPlugin()
    legacy = await plugin._repo(stores).insert(
        chat,
        "The group meets Fridays",
        source="auto",
        pinned=False,
        created_by=alice.ref,
    )
    await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="auto",
        pinned=False,
        created_by=bob.ref,
        source_ids=[(statement.message_id, "statement")],
        provenance="known",
    )

    await plugin._repo(stores).purge_user(chat, alice.ref)

    assert await plugin._repo(stores).get(chat, legacy["id"]) is not None


async def test_automatic_write_rejects_missing_or_bot_source():
    chat = ChatRef("fake", "invalid-source")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    plugin = MemoryPlugin()

    missing = await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="auto",
        pinned=False,
        created_by=UserRef("fake", "alice"),
        epoch=0,
        source_ids=[("missing", "statement")],
        provenance="known",
    )

    assert missing["status"] == "source"
    assert await plugin._repo(stores).list(chat) == []


async def test_automatic_write_rejects_a_source_from_another_chat():
    chat = ChatRef("fake", "target-chat")
    foreign = ChatRef("fake", "foreign-chat")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    actor = user("alice")
    foreign_message = message("foreign source", sender=actor, chat=foreign)
    await stores.repo.log_message(foreign_message)
    plugin = MemoryPlugin()

    result = await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="auto",
        pinned=False,
        created_by=actor.ref,
        epoch=0,
        source_ids=[(foreign_message.message_id, "statement")],
        provenance="known",
    )

    assert result["status"] == "source"
    assert await plugin._repo(stores).list(chat) == []


async def test_pending_action_without_epoch_is_rejected():
    chat = ChatRef("fake", "old-pending")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    actor = user("alice")
    plugin = MemoryPlugin()
    action = PendingAction.create(
        chat=chat,
        requester=actor.ref,
        plugin=plugin.name,
        kind="remember",
        payload={"fact": "The group meets Fridays"},
        summary="old save",
    )
    ctx = CommandContext(chat, actor, False, False, FakePlatform(), stores)

    result = await plugin.action_handlers()["remember"].commit(action, ctx)

    assert result == "This save request is out of date. Please request it again."
    assert await plugin._repo(stores).list(chat) == []


async def test_memory_repo_rejects_writes_while_erasure_is_active_without_epoch():
    chat = ChatRef("fake", "epochless-erasure")
    plugin = MemoryPlugin()
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    repo = plugin._repo(stores)
    repo.erasure_ids[chat.key] = "active-operation"

    result = await repo.commit_fact(
        chat,
        "The group meets Fridays",
        source="explicit",
        pinned=False,
        created_by=UserRef("fake", "alice"),
        subject_user=None,
        candidate_ids=set(),
        dedupe_threshold=0.92,
        epoch=None,
    )

    assert result["status"] == "erasure"
    assert await repo.list(chat) == []


async def test_pending_write_epoch_is_not_refreshed():
    chat = ChatRef("fake", "stale-epoch")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    plugin = MemoryPlugin()
    repo = plugin._repo(stores)
    original = await repo.capture_epoch(chat)
    await repo.bump_epoch(chat)

    result = await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="explicit",
        pinned=False,
        created_by=UserRef("fake", "alice"),
        epoch=original,
    )

    assert result["status"] == "epoch"
    assert await repo.list(chat) == []


async def test_explicit_invalid_source_is_dropped_with_none_provenance():
    chat = ChatRef("fake", "invalid-explicit-source")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    plugin = MemoryPlugin()

    result = await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="explicit",
        pinned=False,
        created_by=UserRef("fake", "alice"),
        epoch=0,
        source_ids=[("missing", "request")],
        provenance="request_only",
    )

    assert result["status"] == "saved"
    assert result["row"]["provenance"] == "none"
    assert plugin._repo(stores).sources[result["row"]["id"]] == []


async def test_erasure_marker_blocks_memory_writes_and_stale_finalizer_cannot_clear_it():
    chat = ChatRef("fake", "active-erasure")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    plugin = MemoryPlugin()
    actor = user("alice")
    await plugin.begin_erasure(chat, "operation-1", "user", actor.ref.key, stores)
    await plugin.finish_erasure(chat, "operation-old", success=True, stores=stores)

    ctx = ToolContext(chat, actor, False, FakePlatform(), stores)
    result = await plugin._remember_tool(ctx, fact="The group meets Fridays")

    assert result.is_error
    assert "paused" in result.content
    assert await plugin._repo(stores).erasure_active(chat)


async def test_forgetme_reports_whole_word_name_matches_without_exposing_text():
    chat = ChatRef("fake", "name-review")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    alice, bob = user("alice"), user("bob")
    await stores.repo.upsert_member(chat, alice)
    plugin = MemoryPlugin()
    saved = await plugin._save(
        chat,
        "Alice prefers the garden table; Malice is a different word.",
        stores,
        source="explicit",
        pinned=False,
        created_by=bob.ref,
    )

    await plugin.purge_user(chat, alice.ref, stores)
    note = await plugin.erasure_notes(chat, alice.ref, stores)

    expected = (
        f"1 surviving memory text(s) match your name or username: "
        f"#{saved['row']['id']}. Use /forget for any you want removed."
    )
    assert note == expected
    assert "Alice prefers" not in note
    assert "Malice" not in note


async def test_forgetme_notes_are_recomputed_from_live_memories():
    chat = ChatRef("fake", "live-name-note")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    alice, bob = user("alice"), user("bob")
    await stores.repo.upsert_member(chat, alice)
    plugin = MemoryPlugin()
    saved = await plugin._save(
        chat,
        "Alice prefers the garden table.",
        stores,
        source="explicit",
        pinned=False,
        created_by=bob.ref,
    )
    await plugin.purge_user(chat, alice.ref, stores)
    assert await plugin.erasure_notes(chat, alice.ref, stores)

    await plugin._repo(stores).delete(chat, int(saved["row"]["id"]), index_enabled=False)

    assert await plugin.erasure_notes(chat, alice.ref, stores) is None


async def test_exact_legacy_row_without_valid_sources_keeps_legacy_provenance():
    chat = ChatRef("fake", "legacy-provenance")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    alice, bob = user("alice"), user("bob")
    plugin = MemoryPlugin()
    repo = plugin._repo(stores)
    legacy = await repo.insert(
        chat,
        "A legacy fact without source links",
        source="auto",
        pinned=False,
        created_by=alice.ref,
    )

    outcome = await repo.commit_fact(
        chat,
        legacy["text"],
        source="explicit",
        pinned=False,
        created_by=bob.ref,
        subject_user=None,
        candidate_ids=set(),
        dedupe_threshold=0.92,
        source_ids=[("expired-request", "request")],
        source_lookup=lambda *_args: _missing_source(),
    )

    assert outcome["status"] == "exact"
    assert outcome["row"]["provenance"] == "legacy_unknown"


async def _missing_source():
    return None


async def test_source_limit_keeps_existing_links_and_reports_omitted_support():
    chat = ChatRef("fake", "source-limit")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    plugin = MemoryPlugin()
    authors = [user(f"person-{i}") for i in range(33)]
    messages = [message("The group meets Fridays", sender=actor, chat=chat) for actor in authors]
    for item in messages:
        await stores.repo.log_message(item)
    saved = await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="auto",
        pinned=False,
        created_by=authors[0].ref,
        epoch=0,
        source_ids=[(item.message_id, "statement") for item in messages[:32]],
        provenance="known",
    )

    reused = await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="auto",
        pinned=False,
        created_by=authors[32].ref,
        epoch=0,
        source_ids=[(messages[32].message_id, "statement")],
        provenance="known",
    )

    assert saved["status"] == "saved"
    assert reused["status"] == "exact"
    assert reused["sources_capped"] is True
    assert len(plugin._repo(stores).sources[saved["row"]["id"]]) == 32


async def test_explicit_similar_fact_keeps_its_request_source():
    chat = ChatRef("fake", "explicit-similar")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    alice = user("alice")
    request = message("Remember that the group meets Fridays", sender=alice, chat=chat)
    await stores.repo.log_message(request)
    plugin = MemoryPlugin()
    await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="auto",
        pinned=False,
        created_by=user("bob").ref,
        epoch=0,
        source_ids=[(request.message_id, "statement")],
        provenance="known",
    )

    similar = await plugin._save(
        chat,
        "The group meets Fridays today",
        stores,
        source="explicit",
        pinned=False,
        created_by=alice.ref,
        epoch=0,
        source_ids=[(request.message_id, "request")],
        provenance="request_only",
    )

    assert similar["status"] == "similar"
    assert plugin._repo(stores).sources[similar["row"]["id"]][0]["source_role"] == "request"


async def test_memory_work_ids_remain_stable_and_purge_chat_keeps_one_chat_delete():
    chat = ChatRef("fake", "work-queue")
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    plugin = MemoryPlugin()
    repo = plugin._repo(stores)
    repo._add_work(chat, 1, "verify_index")
    repo._add_work(chat, 2, "verify_index")
    pending = await repo.index_work(chat)

    await repo.finish_index_work(pending[0]["id"])
    await repo.finish_index_work(pending[1]["id"])

    assert await repo.index_work(chat) == []
    repo._add_work(chat, 3, "delete_doc")
    await repo.purge_chat(chat)
    cleanup = await repo.index_work(chat)
    assert [(item["op"], item["memory_id"]) for item in cleanup] == [("delete_chat", None)]


async def test_purge_chat_preserves_verification_work_for_a_paused_writer():
    chat = ChatRef("fake", "preserve-verification")
    plugin = MemoryPlugin()
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    repo = plugin._repo(stores)
    repo._add_work(chat, 99, "verify_index")
    repo._add_work(chat, 98, "delete_doc")

    await repo.purge_chat(chat)

    assert [(item["op"], item["memory_id"]) for item in await repo.index_work(chat)] == [
        ("verify_index", 99),
        ("delete_chat", None),
    ]


@pytest.mark.parametrize("path", ["forget", "forgetme", "purgechat"])
async def test_erasure_between_commit_and_index_removes_late_document(path):
    chat = ChatRef("fake", f"index-race-{path}")
    search = MemorySearchFake()
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache(), search=search)
    actor = user("alice")
    request = message("Remember the Friday meeting", sender=actor, chat=chat)
    await stores.repo.log_message(request)
    plugin = MemoryPlugin(embedder=FakeEmbedder())
    entered_index, resume_index = asyncio.Event(), asyncio.Event()
    original_index = plugin._index

    async def paused_index(chat_ref, row, stores_arg, *, vector=None):
        entered_index.set()
        await resume_index.wait()
        await original_index(chat_ref, row, stores_arg, vector=vector)

    plugin._index = paused_index
    saving = asyncio.create_task(
        plugin._save(
            chat,
            "The group meets Fridays",
            stores,
            source="explicit",
            pinned=False,
            created_by=actor.ref,
            epoch=0,
            source_ids=[(request.message_id, "request")],
            provenance="request_only",
        )
    )
    await entered_index.wait()
    memory_id = int((await plugin._repo(stores).list(chat))[0]["id"])
    if path == "forget":
        await plugin._delete_memory(chat, memory_id, stores)
    else:
        scope = "user" if path == "forgetme" else "chat"
        target = actor.ref.key if scope == "user" else None
        await plugin.begin_erasure(chat, f"index-race-{path}", scope, target, stores)
        if path == "forgetme":
            await plugin.purge_user(chat, actor.ref, stores)
        else:
            await plugin.purge_chat(chat, stores)
        await plugin.finish_erasure(chat, f"index-race-{path}", success=True, stores=stores)
    resume_index.set()
    await saving

    assert await plugin._repo(stores).list(chat) == []
    assert search.docs == {}
    assert await plugin._repo(stores).index_work(chat, due_only=False) == []


@pytest.mark.parametrize("path", ["forget", "forgetme", "purgechat"])
async def test_late_index_after_cleanup_ran_stays_queued_when_writer_delete_fails(path):
    chat = ChatRef("fake", f"late-index-{path}")
    search = MemorySearchFake()
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache(), search=search)
    actor = user("alice")
    request = message("Remember the Friday meeting", sender=actor, chat=chat)
    await stores.repo.log_message(request)
    plugin = MemoryPlugin(embedder=FakeEmbedder())
    entered_index, resume_index = asyncio.Event(), asyncio.Event()
    original_index = plugin._index

    async def paused_index(chat_ref, row, stores_arg, *, vector=None):
        entered_index.set()
        await resume_index.wait()
        await original_index(chat_ref, row, stores_arg, vector=vector)

    plugin._index = paused_index
    saving = asyncio.create_task(
        plugin._save(
            chat,
            "The group meets Fridays",
            stores,
            source="explicit",
            pinned=False,
            created_by=actor.ref,
            epoch=0,
            source_ids=[(request.message_id, "request")],
            provenance="request_only",
        )
    )
    await entered_index.wait()
    memory_id = int((await plugin._repo(stores).list(chat))[0]["id"])
    if path == "forget":
        await plugin._delete_memory(chat, memory_id, stores)
    else:
        scope = "user" if path == "forgetme" else "chat"
        target = actor.ref.key if scope == "user" else None
        await plugin.begin_erasure(chat, f"late-index-{path}", scope, target, stores)
        if path == "forgetme":
            await plugin.purge_user(chat, actor.ref, stores)
        else:
            await plugin.purge_chat(chat, stores)
        await plugin.finish_erasure(chat, f"late-index-{path}", success=True, stores=stores)
    # Cleanup runs while the writer is still paused and clears the writer's verify row.
    await plugin.process_index_work(stores)
    assert await plugin.pending_cleanup(chat, stores) == 0

    search.fail_delete = True
    resume_index.set()
    await saving

    assert await plugin._repo(stores).list(chat) == []
    assert len(search.docs) == 1
    assert await plugin.pending_cleanup(chat, stores) >= 1

    search.fail_delete = False
    await plugin.process_index_work(stores)
    assert search.docs == {}
    assert await plugin.pending_cleanup(chat, stores) == 0


async def test_failed_index_delete_stays_queued_and_retry_removes_document():
    chat = ChatRef("fake", "delete-retry")
    search = MemorySearchFake()
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache(), search=search)
    actor = user("alice")
    request = message("Remember the Friday meeting", sender=actor, chat=chat)
    await stores.repo.log_message(request)
    plugin = MemoryPlugin(embedder=FakeEmbedder())
    saved = await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="explicit",
        pinned=False,
        created_by=actor.ref,
        epoch=0,
        source_ids=[(request.message_id, "request")],
        provenance="request_only",
    )
    search.fail_delete = True

    assert await plugin._delete_memory(chat, int(saved["row"]["id"]), stores)
    assert len(search.docs) == 1
    assert await plugin.pending_cleanup(chat, stores) == 1

    search.fail_delete = False
    assert await plugin.process_index_work(stores) == 1
    assert search.docs == {}
    assert await plugin.pending_cleanup(chat, stores) == 0


async def test_embedding_failure_saves_fact_and_later_index_pass_recovers_it():
    chat = ChatRef("fake", "embed-retry")
    search = MemorySearchFake()
    embedder = ToggleEmbedder()
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache(), search=search)
    plugin = MemoryPlugin(embedder=embedder)

    saved = await plugin._save(
        chat,
        "The group meets Fridays",
        stores,
        source="explicit",
        pinned=False,
        created_by=user("alice").ref,
    )

    assert saved["status"] == "saved"
    assert await plugin._repo(stores).get(chat, int(saved["row"]["id"])) is not None
    assert [item["op"] for item in await plugin._repo(stores).index_work(chat)] == ["verify_index"]

    embedder.fail = False
    assert await plugin.process_index_work(stores) == 1
    assert list(search.docs.values())[0]["text"] == "The group meets Fridays"


async def test_cleanup_finish_and_count_cover_work_beyond_the_first_page():
    chat = ChatRef("fake", "many-work-items")
    plugin = MemoryPlugin()
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache())
    repo = plugin._repo(stores)
    for memory_id in range(101):
        repo._add_work(chat, memory_id, "delete_doc")

    assert await plugin.pending_cleanup(chat, stores) == 101
    await repo.finish_index_work_for(chat, 100, "delete_doc")

    remaining = await repo.index_work(chat, due_only=False)
    assert len(remaining) == 100
    assert all(item["memory_id"] != 100 for item in remaining)


async def test_index_delete_batches_ids_in_terms_queries():
    class RecordingSearch(MemorySearchFake):
        def __init__(self):
            super().__init__()
            self.deletes = []

        async def delete(self, index, *, chat, query=None, refresh_first=False):
            self.deletes.append((query, refresh_first))
            return 0

    search = RecordingSearch()
    plugin = MemoryPlugin()
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache(), search=search)

    await plugin._delete_index(ChatRef("fake", "batch-delete"), list(range(1201)), stores)

    assert [len(query["terms"]["memory_id"]) for query, _ in search.deletes] == [500, 500, 201]
    assert all(refresh_first for _, refresh_first in search.deletes)
