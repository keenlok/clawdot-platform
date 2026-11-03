from __future__ import annotations

import os
import socket
from pathlib import Path
from uuid import uuid4

import pytest
from chatcore.embed import FakeEmbedder
from chatcore.events import ChatRef, UserRef
from chatcore.store.es import EsSearchIndex
from chatcore.store.pg import PgDatabase
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
        migration = Path(__file__).parents[1] / "src/chatcore_memory/migrations/0001_memories.sql"
        await connection.execute(migration.read_text())
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
