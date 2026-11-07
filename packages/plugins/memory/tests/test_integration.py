from __future__ import annotations

import os
import socket
from pathlib import Path
from uuid import uuid4

import pytest
from chatcore.embed import FakeEmbedder
from chatcore.events import ChatRef, User, UserRef
from chatcore.pending import PendingAction
from chatcore.plugin import CommandContext
from chatcore.privacy import PrivacyPlugin
from chatcore.store.base import Stores
from chatcore.store.es import EsSearchIndex
from chatcore.store.memory import MemoryCache
from chatcore.store.pg import PgCoreRepo, PgDatabase
from chatcore.testing import FakePlatform
from chatcore_memory.plugin import MemoryPlugin
from chatcore_memory.repository import PostgresMemoryRepo


def _pg_dsn() -> str:
    return os.environ.get(
        "TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test"
    )


def _pg_available() -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(_pg_dsn())
    try:
        with socket.create_connection(
            (parsed.hostname or "127.0.0.1", parsed.port or 5432), timeout=0.2
        ):
            return True
    except OSError:
        return False


def _es_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 59200), timeout=0.2):
            return True
    except OSError:
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _pg_available(), reason="Postgres is not reachable")
async def test_postgres_migration_round_trip_and_chat_isolation():
    import asyncpg

    schema = f"memory_test_{uuid4().hex[:12]}"
    connection = await asyncpg.connect(_pg_dsn())
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        for filename in ("0001_memories.sql", "0002_memory_control.sql", "0003_memory_sources.sql"):
            migration = Path(__file__).parents[1] / "src/chatcore_memory/migrations" / filename
            await connection.execute(migration.read_text())
        await connection.execute(
            "CREATE TABLE messages (chat_key text, message_id text, user_key text, "
            "is_bot boolean NOT NULL DEFAULT false, PRIMARY KEY(chat_key,message_id))"
        )
        repo = PostgresMemoryRepo(PgDatabase(connection))
        chat_a, chat_b = ChatRef("test", "a"), ChatRef("test", "b")
        author = UserRef("test", "writer")
        row = await repo.insert(
            chat_a, "chat A only", source="explicit", pinned=False, created_by=author
        )
        assert await repo.get(chat_b, int(row["id"])) is None
        assert (await repo.get(chat_a, int(row["id"])))["text"] == "chat A only"
        await repo.delete(chat_b, int(row["id"]))
        assert await repo.get(chat_a, int(row["id"])) is not None
        await connection.execute(
            "INSERT INTO memory_index_work (chat_key,memory_id,op) VALUES "
            "($1,$2,'verify_index'),($1,$2,'delete_doc')",
            chat_a.key,
            row["id"],
        )
        await repo.purge_chat(chat_a)
        work = await connection.fetch(
            "SELECT memory_id,op FROM memory_index_work WHERE chat_key=$1 ORDER BY id",
            chat_a.key,
        )
        assert [(item["op"], item["memory_id"]) for item in work] == [
            ("verify_index", row["id"]),
            ("delete_chat", None),
        ]
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


@pytest.mark.integration
@pytest.mark.skipif(not _es_available(), reason="Elasticsearch is not reachable")
async def test_es_memory_template_index_hybrid_recall_and_cleanup():
    prefix = f"sb-bot-memory-test-{uuid4().hex[:8]}-"
    concrete = f"{prefix}memories-v1"
    index = EsSearchIndex(es_url="http://127.0.0.1:59200", index_prefix=prefix)
    options = index.client.options(ignore_status=404)
    chat_a, chat_b = ChatRef("test", "a"), ChatRef("test", "b")
    embedder = FakeEmbedder()
    try:
        await index.ensure_templates()
        assert await index.client.indices.exists(index=concrete)
        template = await index.client.indices.get_index_template(name=f"{prefix}memories-v1")
        assert template["index_templates"]
        first, second = await embedder.embed_documents(
            ["Alice likes green tea", "Alice enjoys tea"]
        )
        await index.index(
            "memories",
            "1",
            {
                "memory_id": 1,
                "text": "Alice likes green tea",
                "source": "explicit",
                "pinned": False,
                "created_at": "2026-10-30T00:00:00Z",
                "embedding": first,
            },
            chat=chat_a,
        )
        await index.index(
            "memories",
            "2",
            {
                "memory_id": 2,
                "text": "Other chat green tea",
                "source": "explicit",
                "pinned": False,
                "created_at": "2026-10-30T00:00:00Z",
                "embedding": second,
            },
            chat=chat_b,
        )
        await index.client.indices.refresh(index=concrete)
        hits = await index.search(
            "memories",
            chat=chat_a,
            text="green tea",
            vector=await embedder.embed_query("green tea"),
            size=5,
        )
        assert [hit.doc_id for hit in hits] == ["1"]
        await index.delete("memories", chat=chat_a, query={"term": {"memory_id": 1}})
    finally:
        await options.indices.delete(index=concrete)
        await options.indices.delete(index=f"{prefix}chunks-v1")
        await options.indices.delete_index_template(name=f"{prefix}memories-v1")
        await options.indices.delete_index_template(name=f"{prefix}chunks-v1")


@pytest.mark.integration
@pytest.mark.skipif(not _pg_available(), reason="Postgres is not reachable")
async def test_postgres_concurrent_identical_writes_and_replacements_are_serialized():
    import asyncio

    import asyncpg

    schema = f"memory_race_{uuid4().hex[:12]}"
    setup = await asyncpg.connect(_pg_dsn())
    first = second = None
    try:
        await setup.execute(f'CREATE SCHEMA "{schema}"')
        await setup.execute(f'SET search_path TO "{schema}"')
        for filename in ("0001_memories.sql", "0002_memory_control.sql", "0003_memory_sources.sql"):
            migration = Path(__file__).parents[1] / "src/chatcore_memory/migrations" / filename
            await setup.execute(migration.read_text())
        first, second = await asyncio.gather(asyncpg.connect(_pg_dsn()), asyncpg.connect(_pg_dsn()))
        await first.execute(f'SET search_path TO "{schema}"')
        await second.execute(f'SET search_path TO "{schema}"')
        repo_a, repo_b = (
            PostgresMemoryRepo(PgDatabase(first)),
            PostgresMemoryRepo(PgDatabase(second)),
        )
        chat = ChatRef("test", f"race-{uuid4().hex[:8]}")
        alice = UserRef("test", "alice")
        results = await asyncio.gather(
            repo_a.commit_fact(
                chat,
                "The group meets on Fridays",
                source="auto",
                pinned=False,
                created_by=alice,
                subject_user=None,
                candidate_ids=set(),
                dedupe_threshold=0.92,
            ),
            repo_b.commit_fact(
                chat,
                "The group meets on Fridays",
                source="auto",
                pinned=False,
                created_by=alice,
                subject_user=None,
                candidate_ids=set(),
                dedupe_threshold=0.92,
            ),
        )
        assert {result["status"] for result in results} == {"saved", "exact"}
        assert len(await repo_a.list(chat)) == 1

        old = await repo_a.list(chat)
        original = old[0]
        replacements = await asyncio.gather(
            repo_a.commit_fact(
                chat,
                "The group meets on Saturdays",
                source="explicit",
                pinned=False,
                created_by=alice,
                subject_user=None,
                candidate_ids=set(),
                dedupe_threshold=0.92,
                replaces=int(original["id"]),
                expected_updated_at=original["updated_at"],
            ),
            repo_b.commit_fact(
                chat,
                "The group meets on Sundays",
                source="explicit",
                pinned=False,
                created_by=alice,
                subject_user=None,
                candidate_ids=set(),
                dedupe_threshold=0.92,
                replaces=int(original["id"]),
                expected_updated_at=original["updated_at"],
            ),
        )
        assert sorted(result["status"] for result in replacements) == ["replaced", "stale"]
        assert len(await repo_a.list(chat)) == 1
        assert (await repo_a.list(chat))[0]["text"] in {
            "The group meets on Saturdays",
            "The group meets on Sundays",
        }
    finally:
        if first is not None:
            await first.close()
        if second is not None:
            await second.close()
        await setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await setup.close()


@pytest.mark.integration
@pytest.mark.skipif(not _pg_available(), reason="Postgres is not reachable")
async def _assert_postgres_fence_rejects_paused_writer(*, change_epoch: bool, set_erasure: bool):
    import asyncio

    import asyncpg

    schema = f"memory_fence_{uuid4().hex[:12]}"
    setup = await asyncpg.connect(_pg_dsn())
    first = second = None
    try:
        await setup.execute(f'CREATE SCHEMA "{schema}"')
        await setup.execute(f'SET search_path TO "{schema}"')
        for filename in ("0001_memories.sql", "0002_memory_control.sql", "0003_memory_sources.sql"):
            migration = Path(__file__).parents[1] / "src/chatcore_memory/migrations" / filename
            await setup.execute(migration.read_text())
        first, second = await asyncio.gather(asyncpg.connect(_pg_dsn()), asyncpg.connect(_pg_dsn()))
        await first.execute(f'SET search_path TO "{schema}"')
        await second.execute(f'SET search_path TO "{schema}"')
        repo = PostgresMemoryRepo(PgDatabase(first))
        chat = ChatRef("test", f"fence-{uuid4().hex[:8]}")
        actor = UserRef("test", "writer")
        epoch = await repo.capture_epoch(chat)
        paused, resume = asyncio.Event(), asyncio.Event()

        async def pending_write():
            paused.set()
            await resume.wait()
            return await repo.commit_fact(
                chat,
                "The group meets on Fridays",
                source="explicit",
                pinned=False,
                created_by=actor,
                subject_user=None,
                candidate_ids=set(),
                dedupe_threshold=0.92,
                epoch=epoch,
            )

        writer = asyncio.create_task(pending_write())
        await paused.wait()
        await second.execute(
            "UPDATE memory_control SET epoch=epoch+$2, "
            "erasure_id=CASE WHEN $3 THEN 'operation-fence' ELSE NULL END WHERE chat_key=$1",
            chat.key,
            int(change_epoch),
            set_erasure,
        )
        resume.set()

        result = await writer

        assert result["status"] == ("erasure" if set_erasure else "epoch")
        assert await repo.list(chat) == []
    finally:
        if first is not None:
            await first.close()
        if second is not None:
            await second.close()
        await setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await setup.close()


@pytest.mark.integration
@pytest.mark.skipif(not _pg_available(), reason="Postgres is not reachable")
async def test_postgres_epoch_fence_rejects_a_paused_writer_on_separate_connections():
    await _assert_postgres_fence_rejects_paused_writer(change_epoch=True, set_erasure=False)


@pytest.mark.integration
@pytest.mark.skipif(not _pg_available(), reason="Postgres is not reachable")
async def test_postgres_erasure_fence_rejects_a_paused_writer_on_separate_connections():
    await _assert_postgres_fence_rejects_paused_writer(change_epoch=False, set_erasure=True)


@pytest.mark.integration
@pytest.mark.skipif(not _pg_available(), reason="Postgres is not reachable")
async def test_postgres_legacy_fallback_and_exact_provenance_preservation():
    import asyncpg

    schema = f"memory_legacy_{uuid4().hex[:12]}"
    connection = await asyncpg.connect(_pg_dsn())
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        for filename in ("0001_memories.sql", "0002_memory_control.sql", "0003_memory_sources.sql"):
            migration = Path(__file__).parents[1] / "src/chatcore_memory/migrations" / filename
            await connection.execute(migration.read_text())
        await connection.execute(
            "CREATE TABLE messages (chat_key text, message_id text, user_key text, "
            "is_bot boolean NOT NULL DEFAULT false, PRIMARY KEY(chat_key,message_id))"
        )
        repo = PostgresMemoryRepo(PgDatabase(connection))
        chat = ChatRef("test", f"legacy-{uuid4().hex[:8]}")
        alice, bob = UserRef("test", "alice"), UserRef("test", "bob")

        request_legacy = await repo.insert(
            chat,
            "Legacy fact with request support",
            source="auto",
            pinned=False,
            created_by=alice,
        )
        await connection.execute(
            "INSERT INTO memory_sources (chat_key,memory_id,source_message_id,source_author_key,"
            "source_role,policy_version) VALUES ($1,$2,'bob-request',$3,'request',1)",
            chat.key,
            request_legacy["id"],
            bob.key,
        )
        exact_legacy = await repo.insert(
            chat,
            "Legacy fact with no support",
            source="auto",
            pinned=False,
            created_by=alice,
        )
        shared_legacy = await repo.insert(
            chat,
            "Legacy fact with another author's statement",
            source="auto",
            pinned=False,
            created_by=alice,
        )
        await connection.execute(
            "INSERT INTO memory_sources (chat_key,memory_id,source_message_id,source_author_key,"
            "source_role,policy_version) VALUES ($1,$2,'bob-statement',$3,'statement',1)",
            chat.key,
            shared_legacy["id"],
            bob.key,
        )
        result = await repo.commit_fact(
            chat,
            exact_legacy["text"],
            source="explicit",
            pinned=False,
            created_by=bob,
            subject_user=None,
            candidate_ids=set(),
            dedupe_threshold=0.92,
            source_ids=[("expired-request", "request")],
        )

        assert result["status"] == "exact"
        assert result["row"]["provenance"] == "legacy_unknown"
        deleted = await repo.purge_user(chat, alice)

        assert int(request_legacy["id"]) in deleted
        assert await repo.get(chat, int(exact_legacy["id"])) is None
        assert int(shared_legacy["id"]) not in deleted
        assert await repo.get(chat, int(shared_legacy["id"])) is not None
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


@pytest.mark.integration
@pytest.mark.skipif(not _es_available(), reason="Elasticsearch is not reachable")
async def test_es_delete_refreshes_unrefreshed_document():
    prefix = f"memory-refresh-test-{uuid4().hex[:8]}-"
    index = EsSearchIndex(es_url="http://127.0.0.1:59200", index_prefix=prefix)
    chat = ChatRef("test", "refresh")
    await index.ensure_templates()
    try:
        await index.index(
            "memories",
            "before-refresh",
            {"memory_id": 90210, "text": "unrefreshed document"},
            chat=chat,
        )
        assert (
            await index.delete(
                "memories",
                chat=chat,
                query={"term": {"memory_id": 90210}},
                refresh_first=True,
            )
            == 1
        )
        await index.client.indices.refresh(index=index.index_name("memories"))
        count = await index.client.count(
            index=index.index_name("memories"),
            query={"bool": {"filter": [{"term": {"chat_key": chat.key}}]}},
        )
        assert count["count"] == 0
    finally:
        await index.client.indices.delete(index=f"{prefix}memories-v1", ignore_unavailable=True)
        await index.client.indices.delete(index=f"{prefix}chunks-v1", ignore_unavailable=True)
        await index.client.close()


async def _prepare_erasure_schema(connection, schema: str) -> None:
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    await connection.execute(f'SET search_path TO "{schema}"')
    core_migrations = Path(__file__).parents[3] / "core/src/chatcore/migrations"
    for filename in (
        "0001_core.sql",
        "0002_conversation.sql",
        "0003_administration.sql",
        "0004_access_restrictions.sql",
        "0005_context_reset.sql",
        "0006_conversation_threads.sql",
        "0007_display_time.sql",
        "0008_message_attribution.sql",
        "0009_budget_admin.sql",
        "0010_privacy_erasures.sql",
    ):
        await connection.execute((core_migrations / filename).read_text())
    memory_migrations = Path(__file__).parents[1] / "src/chatcore_memory/migrations"
    for filename in ("0001_memories.sql", "0002_memory_control.sql", "0003_memory_sources.sql"):
        await connection.execute((memory_migrations / filename).read_text())


class _RaceSearch:
    def __init__(self, *, fail_delete_attempt: int | None = None):
        self.documents = {}
        self.fail_delete_attempt = fail_delete_attempt
        self.delete_attempts = 0

    async def index(self, _index, doc_id, source, *, chat):
        self.documents[(chat.key, doc_id)] = dict(source)

    async def delete(self, _index, *, chat, query=None, refresh_first=False):
        self.delete_attempts += 1
        if self.delete_attempts == self.fail_delete_attempt:
            raise ConnectionError("search unavailable")
        if query and "terms" in query:
            ids = set(query["terms"]["memory_id"])
        elif query and "term" in query:
            ids = {query["term"]["memory_id"]}
        else:
            ids = None
        before = len(self.documents)
        self.documents = {
            key: value
            for key, value in self.documents.items()
            if key[0] != chat.key or (ids is not None and int(key[1]) not in ids)
        }
        return before - len(self.documents)


@pytest.mark.integration
@pytest.mark.skipif(not _pg_available(), reason="Postgres is not reachable")
@pytest.mark.parametrize("path", ["forgetme", "purgechat", "forget"])
async def test_postgres_erasure_rejects_writer_paused_before_commit(path, monkeypatch):
    import asyncio

    import asyncpg
    from chatcore_memory.repository import PostgresMemoryRepo

    schema = f"memory_before_commit_{uuid4().hex[:12]}"
    setup = await asyncpg.connect(_pg_dsn())
    writer_conn = eraser_conn = None
    try:
        await _prepare_erasure_schema(setup, schema)
        writer_conn = await asyncpg.connect(_pg_dsn())
        eraser_conn = await asyncpg.connect(_pg_dsn())
        await writer_conn.execute(f'SET search_path TO "{schema}"')
        await eraser_conn.execute(f'SET search_path TO "{schema}"')
        chat = ChatRef("test", f"before-{uuid4().hex[:8]}")
        alice = UserRef("test", "alice")
        writer_db, eraser_db = PgDatabase(writer_conn), PgDatabase(eraser_conn)
        search = _RaceSearch()
        cache = MemoryCache()
        writer_stores = Stores(PgCoreRepo(writer_db), cache, writer_db, search)
        eraser_stores = Stores(PgCoreRepo(eraser_db), cache, eraser_db, search)
        memory = MemoryPlugin(embedder=FakeEmbedder())
        privacy = PrivacyPlugin([memory], provider_label="test")
        repo = PostgresMemoryRepo(writer_db)
        epoch = await repo.capture_epoch(chat)
        seed_id = None
        if path == "forget":
            seed = await memory._save(
                chat,
                "old fact to forget",
                writer_stores,
                source="explicit",
                pinned=False,
                created_by=alice,
            )
            seed_id = int(seed["row"]["id"])

        entered, resume = asyncio.Event(), asyncio.Event()
        original = PostgresMemoryRepo.commit_fact

        async def pause_before_commit(self, *args, **kwargs):
            entered.set()
            await resume.wait()
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(PostgresMemoryRepo, "commit_fact", pause_before_commit)
        writer = asyncio.create_task(
            memory._save(
                chat,
                "new fact after erasure fence",
                writer_stores,
                source="explicit",
                pinned=False,
                created_by=alice,
                epoch=epoch,
            )
        )
        await entered.wait()
        if path == "forget":
            await memory._delete_memory(chat, seed_id, eraser_stores)
        else:
            operation = await eraser_stores.repo.create_erasure(
                f"op-{uuid4().hex[:8]}",
                chat,
                "user" if path == "forgetme" else "chat",
                alice.key if path == "forgetme" else None,
            )
            done, _ = await privacy._process_erasure(operation, eraser_stores, force=True)
            assert done
        resume.set()
        result = await writer
        assert result["status"] == "epoch"
        assert await repo.list(chat) == []
    finally:
        if writer_conn is not None:
            await writer_conn.close()
        if eraser_conn is not None:
            await eraser_conn.close()
        await setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await setup.close()


@pytest.mark.integration
@pytest.mark.skipif(not _pg_available(), reason="Postgres is not reachable")
@pytest.mark.parametrize("path", ["forgetme", "purgechat", "forget"])
@pytest.mark.parametrize("failing_delete", [1, 2], ids=["eraser_delete_fails", "late_delete_fails"])
async def test_postgres_erasure_after_commit_before_indexing_cleans_late_document(
    path, failing_delete, monkeypatch
):
    import asyncio

    import asyncpg

    schema = f"memory_after_commit_{uuid4().hex[:12]}"
    setup = await asyncpg.connect(_pg_dsn())
    writer_conn = eraser_conn = None
    try:
        await _prepare_erasure_schema(setup, schema)
        writer_conn = await asyncpg.connect(_pg_dsn())
        eraser_conn = await asyncpg.connect(_pg_dsn())
        await writer_conn.execute(f'SET search_path TO "{schema}"')
        await eraser_conn.execute(f'SET search_path TO "{schema}"')
        chat = ChatRef("test", f"after-{uuid4().hex[:8]}")
        alice = UserRef("test", "alice")
        writer_db, eraser_db = PgDatabase(writer_conn), PgDatabase(eraser_conn)
        search = _RaceSearch(fail_delete_attempt=failing_delete)
        cache = MemoryCache()
        writer_stores = Stores(PgCoreRepo(writer_db), cache, writer_db, search)
        eraser_stores = Stores(PgCoreRepo(eraser_db), cache, eraser_db, search)
        memory = MemoryPlugin(embedder=FakeEmbedder())
        privacy = PrivacyPlugin([memory], provider_label="test")
        repo = memory._repo(writer_stores)
        source_id = f"source-{uuid4().hex[:8]}"
        await eraser_db.execute(
            "INSERT INTO messages (chat_key,message_id,user_key,display_name,text,sent_at) "
            "VALUES ($1,$2,$3,'Alice','request source',now())",
            chat.key,
            source_id,
            alice.key,
        )
        indexed, resume = asyncio.Event(), asyncio.Event()
        committed_rows = []
        original = memory._index

        async def pause_before_index(chat_arg, row, stores, *, vector=None):
            committed_rows.append(dict(row))
            indexed.set()
            await resume.wait()
            return await original(chat_arg, row, stores, vector=vector)

        monkeypatch.setattr(memory, "_index", pause_before_index)
        writer = asyncio.create_task(
            memory._save(
                chat,
                "late fact must not survive erasure",
                writer_stores,
                source="explicit",
                pinned=False,
                created_by=alice,
                provenance="request_only",
                source_ids=[(source_id, "request")],
            )
        )
        await indexed.wait()
        if path == "forget":
            await memory._delete_memory(chat, int(committed_rows[0]["id"]), eraser_stores)
        else:
            actor = User(alice, "Alice")
            ctx = CommandContext(chat, actor, False, False, FakePlatform(), eraser_stores)
            action = PendingAction.create(
                chat=chat,
                requester=alice,
                plugin="privacy",
                kind="forgetme" if path == "forgetme" else "purgechat",
                payload={"chats": [chat.key]} if path == "forgetme" else {"chat": chat.key},
                summary="delete",
            )
            reply = (
                await privacy._forgetme(action, ctx)
                if path == "forgetme"
                else await privacy._purgechat(action, ctx)
            )
            assert "pending" in reply.lower()
        resume.set()
        await writer
        assert search.delete_attempts >= 2
        await memory.process_index_work(eraser_stores)
        assert search.documents == {}
        assert await repo.list(chat) == []
    finally:
        if writer_conn is not None:
            await writer_conn.close()
        if eraser_conn is not None:
            await eraser_conn.close()
        await setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await setup.close()


@pytest.mark.integration
@pytest.mark.skipif(not _pg_available(), reason="Postgres is not reachable")
@pytest.mark.parametrize("path", ["forgetme", "purgechat", "forget"])
async def test_postgres_cleanup_before_late_index_keeps_failed_writer_delete_queued(
    path, monkeypatch
):
    import asyncio

    import asyncpg

    schema = f"memory_late_index_{uuid4().hex[:12]}"
    setup = await asyncpg.connect(_pg_dsn())
    writer_conn = eraser_conn = None
    try:
        await _prepare_erasure_schema(setup, schema)
        writer_conn = await asyncpg.connect(_pg_dsn())
        eraser_conn = await asyncpg.connect(_pg_dsn())
        await writer_conn.execute(f'SET search_path TO "{schema}"')
        await eraser_conn.execute(f'SET search_path TO "{schema}"')
        chat = ChatRef("test", f"late-{uuid4().hex[:8]}")
        alice = UserRef("test", "alice")
        writer_db, eraser_db = PgDatabase(writer_conn), PgDatabase(eraser_conn)
        search = _RaceSearch()
        cache = MemoryCache()
        writer_stores = Stores(PgCoreRepo(writer_db), cache, writer_db, search)
        eraser_stores = Stores(PgCoreRepo(eraser_db), cache, eraser_db, search)
        memory = MemoryPlugin(embedder=FakeEmbedder())
        privacy = PrivacyPlugin([memory], provider_label="test")
        repo = memory._repo(writer_stores)
        source_id = f"source-{uuid4().hex[:8]}"
        await eraser_db.execute(
            "INSERT INTO messages (chat_key,message_id,user_key,display_name,text,sent_at) "
            "VALUES ($1,$2,$3,'Alice','request source',now())",
            chat.key,
            source_id,
            alice.key,
        )
        indexed, resume = asyncio.Event(), asyncio.Event()
        committed_rows = []
        original = memory._index

        async def pause_before_index(chat_arg, row, stores, *, vector=None):
            committed_rows.append(dict(row))
            indexed.set()
            await resume.wait()
            return await original(chat_arg, row, stores, vector=vector)

        monkeypatch.setattr(memory, "_index", pause_before_index)
        writer = asyncio.create_task(
            memory._save(
                chat,
                "late fact must not survive erasure",
                writer_stores,
                source="explicit",
                pinned=False,
                created_by=alice,
                provenance="request_only",
                source_ids=[(source_id, "request")],
            )
        )
        await indexed.wait()
        if path == "forget":
            await memory._delete_memory(chat, int(committed_rows[0]["id"]), eraser_stores)
        else:
            actor = User(alice, "Alice")
            ctx = CommandContext(chat, actor, False, False, FakePlatform(), eraser_stores)
            action = PendingAction.create(
                chat=chat,
                requester=alice,
                plugin="privacy",
                kind="forgetme" if path == "forgetme" else "purgechat",
                payload={"chats": [chat.key]} if path == "forgetme" else {"chat": chat.key},
                summary="delete",
            )
            if path == "forgetme":
                await privacy._forgetme(action, ctx)
            else:
                await privacy._purgechat(action, ctx)
        # Cleanup runs while the writer is paused and clears the writer's verify row.
        await memory.process_index_work(eraser_stores)
        assert await memory.pending_cleanup(chat, eraser_stores) == 0

        search.fail_delete_attempt = search.delete_attempts + 1
        resume.set()
        await writer
        assert await repo.list(chat) == []
        assert len(search.documents) == 1
        assert await memory.pending_cleanup(chat, eraser_stores) >= 1

        await memory.process_index_work(eraser_stores)
        assert search.documents == {}
        assert await memory.pending_cleanup(chat, eraser_stores) == 0
    finally:
        if writer_conn is not None:
            await writer_conn.close()
        if eraser_conn is not None:
            await eraser_conn.close()
        await setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await setup.close()
