from __future__ import annotations

import json
import os
import socket
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from chatcore.embed import FakeEmbedder
from chatcore.events import ChatRef, User, UserRef
from chatcore.llm.types import Completion, Message, Usage
from chatcore.plugin import CommandContext
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore.testing import FakePlatform, ScriptedProvider, message
from chatcore.tools.registry import ToolContext
from chatcore_memory.plugin import COUNTER_NAMES, COUNTER_TTL_S, MemoryPlugin, counter_key
from chatcore_memory.repository import MemoryMemoryRepo, PostgresMemoryRepo


def context(chat: ChatRef, stores: Stores, speaker: str = "Alice") -> ToolContext:
    actor = User(UserRef(chat.platform, speaker.lower()), speaker, speaker.lower())
    return ToolContext(chat, actor, False, FakePlatform(), stores)


def stores_for_test(*, search=None, cache=None) -> Stores:
    return Stores(repo=MemoryCoreRepo(), cache=cache or MemoryCache(), search=search)


def completion(output: str | dict, *, stop_reason: str = "end_turn") -> Completion:
    text = json.dumps(output) if isinstance(output, dict) else output
    return Completion(Message.assistant(text), stop_reason, Usage(2, 3))


async def counter_value(stores: Stores, name: str, day: date | None = None) -> int:
    raw = await stores.cache.get(counter_key(name, day))
    return int(raw or b"0")


class RaisingCache:
    async def incr(self, *_args, **_kwargs):
        raise ConnectionError("cache failed")

    async def get(self, *_args, **_kwargs):
        raise ConnectionError("cache failed")


class FailedSearch:
    supports_knn = False

    async def index(self, *_args, **_kwargs):
        raise ConnectionError("index failed")

    async def search(self, *_args, **_kwargs):
        return []

    async def delete(self, *_args, **_kwargs):
        return 0


@pytest.mark.parametrize(
    ("name", "response"),
    [
        ("extract_skipped_empty", ""),
        ("extract_skipped_truncated", '{"facts": ["unfinished'),
        ("extract_skipped_wrong_schema", '{"facts": "no"}'),
        ("extract_skipped_invalid_json", "not JSON"),
    ],
)
async def test_response_reason_counters_fire(name, response):
    stores = stores_for_test()
    plugin = MemoryPlugin(provider=ScriptedProvider(responses=[completion(response)]))
    msg = message("A sufficiently long human message for extraction.")
    await plugin.after_turn(msg, msg.text, stores)
    assert await counter_value(stores, "extract_attempted") == 1
    assert await counter_value(stores, name) == 1


async def test_max_tokens_failure_and_success_counters_fire():
    stores = stores_for_test(search=FailedSearch())
    provider = ScriptedProvider(
        responses=[
            completion(
                {"facts": [{"text": "This result was cut short"}]}, stop_reason="max_tokens"
            ),
            completion({"facts": [None, {"text": 4}]}),
            completion({"facts": [{"text": " "}, {"text": "x" * 400}]}),
            completion(
                {
                    "facts": [
                        {"text": "My password is hidden"},
                        {"text": "The group meets every Friday"},
                    ]
                }
            ),
        ]
    )
    plugin = MemoryPlugin(embedder=FakeEmbedder(), provider=provider)
    first = message("A sufficiently long message that receives max tokens.")
    await stores.repo.log_message(first)
    await plugin.after_turn(first, first.text, stores)
    for text in (
        "A second sufficiently long message for facts.",
        "A third sufficiently long message for facts.",
        "A fourth sufficiently long message for facts.",
    ):
        msg = message(text)
        await stores.repo.log_message(msg)
        await plugin.after_turn(msg, msg.text, stores)
    assert await counter_value(stores, "extract_attempted") == 4
    assert await counter_value(stores, "extract_skipped_max_tokens") == 1
    assert await counter_value(stores, "fact_dropped_invalid") == 4
    assert await counter_value(stores, "fact_dropped_sensitive") == 1
    assert await counter_value(stores, "fact_saved") == 1
    assert await counter_value(stores, "index_write_failed") == 1


async def test_ineligible_and_budget_paths_count_only_enabled_extraction():
    stores = stores_for_test()
    provider = ScriptedProvider(responses=[completion({"facts": []})])
    plugin = MemoryPlugin(provider=provider)
    cases = [
        message("short"),
        message("/help should be skipped"),
        message("This bot says hello", sender=User(UserRef("fake", "bot"), "Bot", "bot", True)),
    ]
    for msg in cases:
        await plugin.after_turn(msg, msg.text, stores)
    assert await counter_value(stores, "extract_skipped_ineligible") == 3
    assert await counter_value(stores, "extract_attempted") == 0
    disabled = MemoryPlugin(
        provider=provider,
        settings=MemoryPlugin().settings.model_copy(update={"auto_extract": False}),
    )
    await disabled.after_turn(
        message("A long enough message for this test."),
        "A long enough message for this test.",
        stores,
    )
    assert await counter_value(stores, "extract_skipped_ineligible") == 3
    assert provider.calls == []

    class Exhausted:
        async def budget_exhausted(self):
            return True

    budget_plugin = MemoryPlugin(provider=provider, limits=Exhausted())
    await budget_plugin.after_turn(
        message("A long enough budget test message."), "A long enough budget test message.", stores
    )
    assert await counter_value(stores, "extract_skipped_budget") == 1
    assert await counter_value(stores, "extract_attempted") == 0


async def test_provider_exception_counts_failure_and_counter_failure_is_swallowed():
    class BrokenProvider:
        async def complete(self, *_args, **_kwargs):
            raise RuntimeError("private provider details")

    stores = stores_for_test(cache=RaisingCache())
    plugin = MemoryPlugin(provider=BrokenProvider())
    msg = message("A long enough message to reach the provider.")
    await plugin.after_turn(msg, msg.text, stores)
    healthy = stores_for_test()
    await MemoryPlugin(provider=BrokenProvider()).after_turn(msg, msg.text, healthy)
    assert await counter_value(healthy, "extract_failed") == 1
    ctx = context(msg.chat, stores)
    outcome = await plugin._save(
        msg.chat,
        "A harmless saved fact",
        stores,
        source="auto",
        pinned=False,
        created_by=ctx.user.ref,
    )
    assert outcome["row"]["text"] == "A harmless saved fact"


async def test_save_index_failure_is_counted_and_never_breaks_save():
    chat = ChatRef("fake", "index-failure")
    actor = UserRef("fake", "Alice")
    failing_stores = stores_for_test(search=FailedSearch(), cache=RaisingCache())
    failing_plugin = MemoryPlugin(embedder=FakeEmbedder())
    outcome = await failing_plugin._save(
        chat,
        "The group keeps a spare lantern",
        failing_stores,
        source="auto",
        pinned=False,
        created_by=actor,
    )
    assert outcome["row"]["text"] == "The group keeps a spare lantern"

    working_stores = stores_for_test(search=FailedSearch())
    working_plugin = MemoryPlugin(embedder=FakeEmbedder())
    outcome = await working_plugin._save(
        chat,
        "The group keeps a spare compass",
        working_stores,
        source="auto",
        pinned=False,
        created_by=actor,
    )
    assert outcome["row"]["text"] == "The group keeps a spare compass"
    assert await counter_value(working_stores, "index_write_failed") == 1


async def test_counter_names_and_ttl_are_fixed():
    expected = {
        "extract_attempted",
        "extract_failed",
        "extract_skipped_ineligible",
        "extract_skipped_synthetic",
        "fact_dropped_unlinked",
        "fact_reused",
        "skipped_near_duplicate",
        "extract_skipped_budget",
        "extract_skipped_max_tokens",
        "extract_skipped_empty",
        "extract_skipped_truncated",
        "extract_skipped_wrong_schema",
        "extract_skipped_invalid_json",
        "fact_dropped_invalid",
        "fact_dropped_sensitive",
        "fact_saved",
        "index_write_failed",
        "fact_rejected_source",
        "fact_rejected_epoch",
        "fact_sources_capped",
        "extract_skipped_erasure",
    }
    assert set(COUNTER_NAMES) == expected
    assert COUNTER_TTL_S == 40 * 86400
    assert counter_key("fact_saved", date(2026, 1, 2)) == "memory_metrics:2026-01-02:fact_saved"


async def test_memorystats_is_admin_chat_scoped_bounded_and_read_only():
    plugin = MemoryPlugin()
    assert next(
        command for command in plugin.commands() if command.name == "memorystats"
    ).admin_only
    repo = MemoryMemoryRepo()
    stores = stores_for_test()
    plugin._memory_repo = repo
    chat_a, chat_b = ChatRef("fake", "a"), ChatRef("fake", "b")
    author = UserRef("fake", "Alice")
    now = datetime.now(UTC)
    for index in range(55):
        row = await repo.insert(
            chat_a, f"private fact {index}", source="auto", pinned=False, created_by=author
        )
        repo.rows[int(row["id"])]["updated_at"] = now + timedelta(seconds=1)
    other = await repo.insert(
        chat_b, "other chat private fact", source="auto", pinned=False, created_by=author
    )
    repo.rows[int(other["id"])]["updated_at"] = now + timedelta(seconds=1)
    for day_index in range(14):
        await stores.cache.incr(
            counter_key("fact_saved", datetime.now(UTC).date() - timedelta(days=day_index)),
            ttl_s=COUNTER_TTL_S,
        )

    command = next(command for command in plugin.commands() if command.name == "memorystats")
    user = User(author, "Alice", "alice")
    ctx = CommandContext(chat_a, user, False, True, FakePlatform(), stores)
    reply = await command.handler(ctx, "")
    assert "fact_saved" in reply and "today=1" in reply and "14d=14" in reply
    assert "Overwritten automatic rows in this chat: 55" in reply
    assert "IDs (up to 50):" in reply and "5 more not shown" in reply
    assert "other chat private fact" not in reply
    assert "private fact" not in reply
    assert len(repo.rows) == 56


async def test_memorystats_treats_failed_counter_reads_as_zero():
    plugin = MemoryPlugin()
    stores = stores_for_test(cache=RaisingCache())
    command = next(command for command in plugin.commands() if command.name == "memorystats")
    actor = User(UserRef("fake", "Alice"), "Alice", "alice")
    ctx = CommandContext(ChatRef("fake", "empty"), actor, False, True, FakePlatform(), stores)
    reply = await command.handler(ctx, "")
    assert "extract_attempted today=0 14d=0" in reply
    assert "Overwritten automatic rows in this chat: 0" in reply


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


@pytest.mark.integration
@pytest.mark.skipif(not _pg_available(), reason="Postgres is not reachable")
async def test_postgres_audit_overwritten_is_chat_scoped_and_ascending():
    import asyncpg
    from chatcore.store.pg import PgDatabase

    schema = f"memory_audit_{uuid4().hex[:12]}"
    connection = await asyncpg.connect(_pg_dsn())
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        migration = Path(__file__).parents[1] / "src/chatcore_memory/migrations/0001_memories.sql"
        await connection.execute(migration.read_text())
        repo = PostgresMemoryRepo(PgDatabase(connection))
        chat_a, chat_b = ChatRef("test", "a"), ChatRef("test", "b")
        author = UserRef("test", "writer")
        first = await repo.insert(chat_a, "first", source="auto", pinned=False, created_by=author)
        second = await repo.insert(chat_a, "second", source="auto", pinned=False, created_by=author)
        await repo.insert(chat_b, "elsewhere", source="auto", pinned=False, created_by=author)
        await connection.execute(
            "UPDATE memories SET updated_at=created_at + interval '1 second' WHERE id IN ($1,$2)",
            first["id"],
            second["id"],
        )
        assert await repo.audit_overwritten(chat_a) == sorted([int(first["id"]), int(second["id"])])
        assert await repo.audit_overwritten(chat_b) == []
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
