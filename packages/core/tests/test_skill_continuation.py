import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import resources
from unittest.mock import AsyncMock

import asyncpg
import pytest
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.attribution import render_record, turn_record
from chatcore.conversation import ConversationStore, Turn
from chatcore.events import ChatRef
from chatcore.limits import Limits
from chatcore.llm.types import Completion, Message
from chatcore.plugin import BasePlugin
from chatcore.skill_turn import SkillSettings
from chatcore.skills import Gate, GateState, Skill, SkillsMode, ToolGate
from chatcore.store import migrations
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache
from chatcore.store.pg import PgCoreRepo, PgDatabase
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message, user

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@asynccontextmanager
async def _postgres_skill_store():
    if not TEST_PG_DSN:
        pytest.skip("TEST_PG_DSN is not configured")
    try:
        admin = await asyncpg.connect(TEST_PG_DSN, timeout=3)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres is unavailable: {type(exc).__name__}")
    schema = f"skill_cont_{uuid.uuid4().hex[:10]}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = None
    db = None
    try:
        pool = await asyncpg.create_pool(
            TEST_PG_DSN,
            min_size=1,
            max_size=2,
            timeout=3,
            server_settings={"search_path": schema},
        )
        db = PgDatabase(pool)
        await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
        repo = PgCoreRepo(db)
        yield db, repo, ConversationStore(db)
    finally:
        if db is not None:
            await db.close()
        elif pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_memory_skill_state_requires_requester_and_live_trigger_message():
    stores = memory_stores()
    store = ConversationStore()
    incoming = message("remind me")
    await stores.repo.log_message(incoming)
    now = datetime.now(UTC)
    state = {
        "requester": incoming.user.ref.key,
        "skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}],
    }
    await store.add(
        GROUP, None, "assistant", {"text": "ok", "skill_state": state}, incoming.message_id
    )
    found = await store.latest_skill_state(
        GROUP,
        thread_id=None,
        conversation_id=None,
        requester=incoming.user.ref.key,
        since=now - timedelta(minutes=30),
        repo=stores.repo,
    )
    assert found == state
    assert (
        await store.latest_skill_state(
            GROUP,
            thread_id=None,
            conversation_id=None,
            requester=user("bob").ref.key,
            since=now - timedelta(minutes=30),
            repo=stores.repo,
        )
        is None
    )

    class MissingMessageRepo:
        async def get_message(self, chat, message_id):
            return None

    assert (
        await store.latest_skill_state(
            GROUP,
            thread_id=None,
            conversation_id=None,
            requester=incoming.user.ref.key,
            since=now - timedelta(minutes=30),
            repo=MissingMessageRepo(),
        )
        is None
    )


async def test_malformed_skill_state_is_a_miss():
    stores = memory_stores()
    store = ConversationStore()
    incoming = message("hello")
    await stores.repo.log_message(incoming)
    now = datetime.now(UTC)
    bad = {
        "requester": incoming.user.ref.key,
        "skills": [{"name": "reminders", "left": -1, "fresh_at": "yesterday"}],
    }
    await store.add(GROUP, None, "assistant", {"skill_state": bad}, incoming.message_id)
    found = await store.latest_skill_state(
        GROUP,
        thread_id=None,
        conversation_id=None,
        requester=incoming.user.ref.key,
        since=now - timedelta(minutes=30),
        repo=stores.repo,
    )
    assert found == {"requester": incoming.user.ref.key, "skills": []}


async def test_legacy_assistant_state_without_requester_and_expired_state_are_misses():
    stores = memory_stores()
    incoming = message("old turn")
    await stores.repo.log_message(incoming)
    now = datetime.now(UTC)
    legacy = {"skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}]}
    stale_store = ConversationStore()
    stale_store._turns.append(
        Turn(1, GROUP, None, "assistant", {"skill_state": legacy}, incoming.message_id, now)
    )
    assert (
        await stale_store.latest_skill_state(
            GROUP,
            thread_id=None,
            conversation_id=None,
            requester=incoming.user.ref.key,
            since=now - timedelta(minutes=30),
            repo=stores.repo,
        )
        is None
    )
    expired = {
        "requester": incoming.user.ref.key,
        "skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}],
    }
    stale_store._turns.append(
        Turn(
            2,
            GROUP,
            None,
            "assistant",
            {"skill_state": expired},
            incoming.message_id,
            now - timedelta(minutes=31),
        )
    )
    assert (
        await stale_store.latest_skill_state(
            GROUP,
            thread_id=None,
            conversation_id=None,
            requester=incoming.user.ref.key,
            since=now - timedelta(minutes=30),
            repo=stores.repo,
        )
        is None
    )


async def test_memory_state_read_requires_repo():
    with pytest.raises(ValueError, match="repo is required"):
        await ConversationStore().latest_skill_state(
            GROUP,
            thread_id=None,
            conversation_id=None,
            requester="fake:user",
            since=datetime.now(UTC) - timedelta(minutes=30),
        )


async def test_memory_continuation_is_requester_and_chat_scoped():
    stores = memory_stores()
    store = ConversationStore()
    now = datetime.now(UTC)
    alice = message("alice turn", sender=user("alice"), thread_id="topic")
    bob = message("bob turn", sender=user("bob"), thread_id="topic")
    other_chat = message("other chat", sender=user("alice"), chat=ChatRef("fake", "other"))
    for incoming, skill in ((alice, "reminders"), (bob, "polls"), (other_chat, "expenses")):
        await stores.repo.log_message(incoming)
        await store.add(
            incoming.chat,
            incoming.thread_id,
            "assistant",
            {
                "skill_state": {
                    "requester": incoming.user.ref.key,
                    "skills": [{"name": skill, "left": 2, "fresh_at": now.isoformat()}],
                }
            },
            incoming.message_id,
        )

    async def read(chat, thread, requester):
        return await store.latest_skill_state(
            chat,
            thread_id=thread,
            conversation_id=None,
            requester=requester,
            since=now - timedelta(minutes=30),
            repo=stores.repo,
        )

    assert (await read(GROUP, "topic", alice.user.ref.key))["skills"][0]["name"] == "reminders"
    assert (await read(GROUP, "topic", bob.user.ref.key))["skills"][0]["name"] == "polls"
    assert (await read(ChatRef("fake", "other"), None, alice.user.ref.key))["skills"][0][
        "name"
    ] == "expenses"
    assert await read(ChatRef("fake", "missing"), None, alice.user.ref.key) is None


@pytest.mark.parametrize("purge", ["purge_user", "purge_chat"])
@pytest.mark.parametrize("cache_mode", ["available", "raising"])
async def test_pause_after_read_then_purge_prevents_late_skill_state_from_continuing(
    purge, cache_mode
):
    stores = memory_stores()
    if cache_mode == "raising":

        class BrokenCache:
            def __getattribute__(self, _name):
                raise RuntimeError("cache access is forbidden in this test")

        stores.cache = BrokenCache()
    store = ConversationStore()
    now = datetime.now(UTC)
    previous = message("prior request")
    await stores.repo.log_message(previous)
    previous_state = {
        "requester": previous.user.ref.key,
        "skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}],
    }
    await store.add(GROUP, None, "assistant", {"skill_state": previous_state}, previous.message_id)
    triggering = message("make it later", sender=previous.user)
    await stores.repo.log_message(triggering)
    ready = asyncio.Event()
    release = asyncio.Event()

    async def paused_turn():
        read = await store.latest_skill_state(
            GROUP,
            thread_id=None,
            conversation_id=None,
            requester=triggering.user.ref.key,
            since=now - timedelta(minutes=30),
            repo=stores.repo,
        )
        assert read == previous_state
        ready.set()
        await release.wait()
        await store.add(
            GROUP,
            None,
            "assistant",
            {"skill_state": read},
            triggering.message_id,
        )

    task = asyncio.create_task(paused_turn())
    await ready.wait()
    if purge == "purge_user":
        await stores.repo.purge_user(GROUP, triggering.user.ref)
    else:
        await stores.repo.purge_chat(GROUP)
    release.set()
    await task
    assert (
        await store.latest_skill_state(
            GROUP,
            thread_id=None,
            conversation_id=None,
            requester=triggering.user.ref.key,
            since=now - timedelta(minutes=30),
            repo=stores.repo,
        )
        is None
    )


async def test_memory_reset_ends_skill_continuation():
    stores = memory_stores()
    store = ConversationStore()
    incoming = message("prior request")
    await stores.repo.log_message(incoming)
    now = datetime.now(UTC)
    state = {
        "requester": incoming.user.ref.key,
        "skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}],
    }
    await store.add(GROUP, None, "assistant", {"skill_state": state}, incoming.message_id)
    await store.reset(GROUP, None, by=incoming.user.ref, at=now + timedelta(seconds=1))
    assert (
        await store.latest_skill_state(
            GROUP,
            thread_id=None,
            conversation_id=None,
            requester=incoming.user.ref.key,
            since=now - timedelta(minutes=30),
            repo=stores.repo,
        )
        is None
    )


async def test_skill_state_is_excluded_from_compaction_and_next_transcript():
    stores = memory_stores()
    store = ConversationStore()
    incoming = message("remember my preference")
    await stores.repo.log_message(incoming)
    await store.add(
        GROUP,
        None,
        "user",
        {"text": incoming.text, "raw_text": incoming.text},
        incoming.message_id,
    )
    state = {
        "requester": incoming.user.ref.key,
        "skills": [{"name": "memory", "left": 2, "fresh_at": datetime.now(UTC).isoformat()}],
    }
    assistant = await store.add(
        GROUP,
        None,
        "assistant",
        {"text": "Saved.", "skill_state": state},
        incoming.message_id,
    )
    transcript_record = render_record(turn_record(assistant), max_chars=1000)
    assert "skill_state" not in transcript_record
    provider = ScriptedProvider([Completion(Message.assistant("summary"), "end_turn")])
    await store.compact(GROUP, None, provider, "rules", "fake", force=True, keep=0)
    serialized_summary_request = str(provider.calls[0]["messages"])
    assert "skill_state" not in serialized_summary_request
    next_transcript = "\n".join(
        render_record(turn_record(row), max_chars=1000) for row in await store.recent(GROUP, None)
    )
    assert "skill_state" not in next_transcript


async def test_new_or_other_requester_conversation_does_not_inherit_state():
    stores = memory_stores()
    store = ConversationStore()
    now = datetime.now(UTC)
    alice = user("alice")
    bob = user("bob")
    alice_conversation = await store.create_conversation(GROUP, "topic", alice.ref, at=now)
    bob_conversation = await store.create_conversation(GROUP, "topic", bob.ref, at=now)
    incoming = message("remind me", sender=alice, thread_id="topic")
    await stores.repo.log_message(incoming)
    await store.add(
        GROUP,
        "topic",
        "assistant",
        {
            "skill_state": {
                "requester": alice.ref.key,
                "skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}],
            }
        },
        incoming.message_id,
        conversation_id=alice_conversation.id,
    )

    async def read(conversation_id, requester):
        return await store.latest_skill_state(
            GROUP,
            thread_id="topic",
            conversation_id=conversation_id,
            requester=requester,
            since=now - timedelta(minutes=30),
            repo=stores.repo,
        )

    assert (await read(alice_conversation.id, alice.ref.key))["skills"][0]["name"] == "reminders"
    assert await read(bob_conversation.id, bob.ref.key) is None
    new_conversation = await store.create_conversation(GROUP, "topic", alice.ref, at=now)
    assert await read(new_conversation.id, alice.ref.key) is None


@pytest.mark.integration
async def test_postgres_latest_skill_state_uses_a_scratch_schema():
    if not TEST_PG_DSN:
        pytest.skip("TEST_PG_DSN is not configured")
    try:
        admin = await asyncpg.connect(TEST_PG_DSN, timeout=3)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres is unavailable: {type(exc).__name__}")
    schema = f"skill_state_{uuid.uuid4().hex[:10]}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        TEST_PG_DSN,
        min_size=1,
        max_size=2,
        timeout=3,
        server_settings={"search_path": schema},
    )
    db = PgDatabase(pool)
    try:
        await migrations.apply(db, [("chatcore", resources.files("chatcore.migrations"))])
        repo = PgCoreRepo(db)
        store = ConversationStore(db)
        chat = ChatRef("fake", "postgres-skill-state")
        incoming = message("remind me", chat=chat)
        await repo.log_message(incoming)
        now = datetime.now(UTC)
        state = {
            "requester": incoming.user.ref.key,
            "skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}],
        }
        await store.add(
            chat,
            incoming.thread_id,
            "assistant",
            {"skill_state": state},
            incoming.message_id,
        )
        found = await store.latest_skill_state(
            chat,
            thread_id=incoming.thread_id,
            conversation_id=None,
            requester=incoming.user.ref.key,
            since=now - timedelta(minutes=30),
            repo=repo,
        )
        assert found == state
        await repo.purge_user(chat, incoming.user.ref)
        assert (
            await store.latest_skill_state(
                chat,
                thread_id=incoming.thread_id,
                conversation_id=None,
                requester=incoming.user.ref.key,
                since=now - timedelta(minutes=30),
                repo=repo,
            )
            is None
        )
    finally:
        await db.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.integration
async def test_postgres_alternating_requesters_keep_skill_state_after_tool_heavy_turns():
    async with _postgres_skill_store() as (_db, repo, store):
        now = datetime.now(UTC)
        alice = user("alice")
        bob = user("bob")
        alice_message = message("alice reminder", sender=alice, thread_id="topic")
        await repo.log_message(alice_message)
        alice_state = {
            "requester": alice.ref.key,
            "skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}],
        }
        await store.add(
            GROUP, "topic", "assistant", {"skill_state": alice_state}, alice_message.message_id
        )
        bob_message = message("bob poll", sender=bob, thread_id="topic")
        await repo.log_message(bob_message)
        await store.add(
            GROUP,
            "topic",
            "assistant",
            {"text": "calling tools", "tool_calls": [{"name": "read_chat_history"}]},
            bob_message.message_id,
        )
        for index in range(3):
            await store.add(
                GROUP,
                "topic",
                "tool",
                {"tool_call_id": f"bob-tool-{index}", "text": "ok"},
                bob_message.message_id,
            )
        bob_state = {
            "requester": bob.ref.key,
            "skills": [{"name": "polls", "left": 2, "fresh_at": now.isoformat()}],
        }
        await store.add(
            GROUP, "topic", "assistant", {"skill_state": bob_state}, bob_message.message_id
        )

        async def read(requester):
            return await store.latest_skill_state(
                GROUP,
                thread_id="topic",
                conversation_id=None,
                requester=requester,
                since=now - timedelta(minutes=30),
                repo=repo,
            )

        assert (await read(alice.ref.key))["skills"][0]["name"] == "reminders"
        assert (await read(bob.ref.key))["skills"][0]["name"] == "polls"


@pytest.mark.integration
async def test_postgres_reset_and_new_conversation_end_skill_continuation():
    async with _postgres_skill_store() as (_db, repo, store):
        now = datetime.now(UTC)
        requester = user("reset-user")

        async def write(chat, thread, *, conversation_id=None):
            incoming = message("prior", chat=chat, thread_id=thread, sender=requester)
            await repo.log_message(incoming)
            state = {
                "requester": requester.ref.key,
                "skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}],
            }
            await store.add(
                chat,
                thread,
                "assistant",
                {"skill_state": state},
                incoming.message_id,
                conversation_id=conversation_id,
            )
            return incoming

        async def read(chat, thread, conversation_id=None):
            return await store.latest_skill_state(
                chat,
                thread_id=thread,
                conversation_id=conversation_id,
                requester=requester.ref.key,
                since=now - timedelta(minutes=30),
                repo=repo,
            )

        dm = ChatRef("fake", "skill-dm")
        await write(dm, None)
        assert await read(dm, None)
        await store.reset(dm, None, by=requester.ref, at=now + timedelta(seconds=1))
        assert await read(dm, None) is None

        await write(GROUP, "topic")
        assert await read(GROUP, "topic")
        await store.reset(GROUP, "topic", by=requester.ref, at=now + timedelta(seconds=1))
        assert await read(GROUP, "topic") is None

        conversation = await store.create_conversation(GROUP, "topic", requester.ref, at=now)
        await write(GROUP, "topic", conversation_id=conversation.id)
        next_conversation = await store.create_conversation(
            GROUP, "topic", requester.ref, at=now + timedelta(seconds=2)
        )
        assert await read(GROUP, "topic", next_conversation.id) is None


@pytest.mark.integration
async def test_postgres_malformed_and_legacy_skill_records_are_misses():
    async with _postgres_skill_store() as (_db, repo, store):
        now = datetime.now(UTC)
        incoming = message("check malformed", chat=ChatRef("fake", "skill-malformed"))
        await repo.log_message(incoming)
        await store.add(
            incoming.chat,
            None,
            "assistant",
            {"skill_state": {"skills": [{"name": "reminders"}]}},
            incoming.message_id,
        )
        since = now - timedelta(minutes=30)
        params = dict(
            thread_id=None,
            conversation_id=None,
            requester=incoming.user.ref.key,
            since=since,
            repo=repo,
        )
        assert await store.latest_skill_state(incoming.chat, **params) is None
        await store.add(
            incoming.chat,
            None,
            "assistant",
            {
                "skill_state": {
                    "requester": incoming.user.ref.key,
                    "skills": "not-a-list",
                }
            },
            incoming.message_id,
        )
        assert await store.latest_skill_state(incoming.chat, **params) is None


@pytest.mark.integration
async def test_postgres_skill_state_older_than_thirty_minutes_is_a_miss():
    async with _postgres_skill_store() as (db, repo, store):
        incoming = message("expire", chat=ChatRef("fake", "skill-expiry"))
        await repo.log_message(incoming)
        now = datetime.now(UTC)
        saved = await store.add(
            incoming.chat,
            None,
            "assistant",
            {
                "skill_state": {
                    "requester": incoming.user.ref.key,
                    "skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}],
                }
            },
            incoming.message_id,
        )
        await db.execute(
            "UPDATE conversation_turns SET created_at=now() - interval '31 minutes' WHERE id=$1",
            saved.id,
        )
        assert (
            await store.latest_skill_state(
                incoming.chat,
                thread_id=None,
                conversation_id=None,
                requester=incoming.user.ref.key,
                since=now - timedelta(minutes=30),
                repo=repo,
            )
            is None
        )


@pytest.mark.integration
@pytest.mark.parametrize("purge", ["purge_user", "purge_chat"])
@pytest.mark.parametrize("cache_mode", ["memory", "raising"])
async def test_postgres_purge_race_blocks_late_skill_state(purge, cache_mode):
    async with _postgres_skill_store() as (_db, repo, store):
        now = datetime.now(UTC)
        previous = message("prior", chat=ChatRef("fake", "skill-purge-race"))
        await repo.log_message(previous)
        state = {
            "requester": previous.user.ref.key,
            "skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}],
        }
        await store.add(
            previous.chat, None, "assistant", {"skill_state": state}, previous.message_id
        )
        current = message("followup", chat=previous.chat, sender=previous.user)
        await repo.log_message(current)
        ready = asyncio.Event()
        release = asyncio.Event()

        class RaisingCache:
            def __getattr__(self, _name):
                raise AssertionError("continuation accessed the cache")

        cache = MemoryCache(prefix="skill-pg:") if cache_mode == "memory" else RaisingCache()
        _stores = Stores(repo=repo, cache=cache)
        assert _stores.repo is repo

        async def paused_read_then_write():
            found = await store.latest_skill_state(
                current.chat,
                thread_id=None,
                conversation_id=None,
                requester=current.user.ref.key,
                since=now - timedelta(minutes=30),
                repo=repo,
            )
            assert found == state
            ready.set()
            await release.wait()
            await store.add(
                current.chat,
                None,
                "assistant",
                {"skill_state": found},
                current.message_id,
            )

        task = asyncio.create_task(paused_read_then_write())
        await ready.wait()
        if purge == "purge_user":
            await repo.purge_user(current.chat, current.user.ref)
        else:
            await repo.purge_chat(current.chat)
        release.set()
        await task
        assert (
            await store.latest_skill_state(
                current.chat,
                thread_id=None,
                conversation_id=None,
                requester=current.user.ref.key,
                since=now - timedelta(minutes=30),
                repo=repo,
            )
            is None
        )


def _memory_agent(stores, plugins, provider, *, general_tools=()):
    from chatcore.conversation import ConversationPlugin

    store = ConversationStore()
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[ConversationPlugin(store), *plugins],
        general_tools=general_tools,
        bot_name="testbot",
    )
    responder = AgentResponder(
        provider,
        system_prompt="legacy",
        skills_prompt="skills",
        settings=AgentSettings(
            skills=SkillSettings(mode=SkillsMode.ON, chats=frozenset({"*"}), router="keyword")
        ),
        limits=Limits(stores.cache),
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )
    app.responder = responder
    return app, responder, store


async def _agent_turn(app, responder, stores, incoming):
    await stores.repo.log_message(incoming)
    await responder.respond(incoming, incoming.text, app)
    return await responder._store(app).recent(incoming.chat, incoming.thread_id)


class _RevocablePlugin(BasePlugin):
    name = "revocable"

    def __init__(self):
        self.enabled = True

    def tools(self):
        from chatcore.tools.registry import Tool, ToolResult

        async def read(_ctx):
            return ToolResult("read")

        return [
            Tool("read_preference", "Read preference", {"type": "object", "properties": {}}, read)
        ]

    def skills(self):
        return [
            Skill(
                "preferences",
                "Read preferences",
                ("read my preferences", "查看偏好", "check my prefs", "preferences now"),
                ("read_preference",),
                keywords=("preference", "prefs"),
            )
        ]

    def tool_gates(self):
        async def gate(_chat, _is_dm):
            return Gate(GateState.AVAILABLE if self.enabled else GateState.OFF)

        return {"read_preference": ToolGate(scope=gate)}


@pytest.mark.asyncio
async def test_revoked_gate_drops_inherited_tool_on_next_agent_request():
    plugin = _RevocablePlugin()
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("ok"), "end_turn"),
            Completion(Message.assistant("ok"), "end_turn"),
        ]
    )
    stores = memory_stores()
    app, responder, _store = _memory_agent(stores, [plugin], provider)
    first = message("read preference")
    await _agent_turn(app, responder, stores, first)
    assert "read_preference" in {tool.name for tool in provider.calls[0]["tools"]}
    plugin.enabled = False
    second = message("and now?", sender=first.user)
    await _agent_turn(app, responder, stores, second)
    assert "read_preference" not in {tool.name for tool in provider.calls[1]["tools"]}


@pytest.mark.asyncio
async def test_jobs_toggle_from_topic_a_is_seen_in_topic_b_on_next_turn():
    from chatcore_jobs.models import JobKind, Scope
    from chatcore_jobs.plugin import JobsPlugin
    from chatcore_jobs.settings import JobsSettings

    class MemoryJobSettings:
        def __init__(self):
            self.values = {}

        async def settings(self, scope):
            values = self.values.get(scope.chat_key, {})
            return {
                "reminders_enabled": values.get(JobKind.REMINDER, True),
                "watches_enabled": values.get(JobKind.WATCH, True),
            }

        async def configure(self, scope, *, kind, enabled):
            self.values.setdefault(scope.chat_key, {})[kind] = enabled

    class MemoryDb:
        async def fetchrow(self, *_args):
            return None

    stores = memory_stores()
    stores.db = MemoryDb()
    platform = FakePlatform()
    jobs = JobsPlugin(
        JobsSettings(),
        AccessPolicy([GROUP.key], []),
        platform,
        sender=AsyncMock(),
    )
    settings_store = MemoryJobSettings()
    jobs.service.repo = lambda _stores: settings_store
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("ok"), "end_turn"),
            Completion(Message.assistant("ok"), "end_turn"),
        ]
    )
    app, responder, _store = _memory_agent(stores, [jobs], provider)
    jobs._stores = stores
    topic_a = message("remind me tomorrow", thread_id="topic-a")
    await _agent_turn(app, responder, stores, topic_a)
    assert "create_reminder" in {tool.name for tool in provider.calls[0]["tools"]}
    await settings_store.configure(Scope(GROUP.key, False), kind=JobKind.REMINDER, enabled=False)
    topic_b = message("remind me tomorrow", sender=topic_a.user, thread_id="topic-b")
    await _agent_turn(app, responder, stores, topic_b)
    assert "create_reminder" not in {tool.name for tool in provider.calls[1]["tools"]}


@pytest.mark.asyncio
async def test_dm_group_link_change_adds_and_removes_expense_tools():
    from chatcore_expenses.plugin import ExpensesPlugin
    from chatcore_expenses.repository import MemoryLedgerRepo

    linked_group = GROUP

    async def group_for_dm(_ctx):
        return linked_group

    plugin = ExpensesPlugin(group_for_dm=group_for_dm, ledger=MemoryLedgerRepo())
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("ok"), "end_turn"),
            Completion(Message.assistant("ok"), "end_turn"),
        ]
    )
    stores = memory_stores()
    app, responder, _store = _memory_agent(stores, [plugin], provider)
    first = message("check the balance", dm=True)
    await _agent_turn(app, responder, stores, first)
    assert "get_balances" in {tool.name for tool in provider.calls[0]["tools"]}
    linked_group = None
    second = message("and now?", sender=first.user, dm=True)
    await _agent_turn(app, responder, stores, second)
    assert "get_balances" not in {tool.name for tool in provider.calls[1]["tools"]}


@pytest.mark.asyncio
async def test_jobs_toggle_in_group_a_does_not_change_group_b_tools():
    from chatcore_jobs.models import JobKind, Scope
    from chatcore_jobs.plugin import JobsPlugin
    from chatcore_jobs.settings import JobsSettings

    class MemoryJobSettings:
        def __init__(self):
            self.values = {}

        async def settings(self, scope):
            return {
                "reminders_enabled": self.values.get(scope.chat_key, {}).get(
                    JobKind.REMINDER, True
                ),
                "watches_enabled": True,
            }

        async def configure(self, scope, *, kind, enabled):
            self.values.setdefault(scope.chat_key, {})[kind] = enabled

    class MemoryDb:
        async def fetchrow(self, *_args):
            return None

    group_a = ChatRef("fake", "group-a")
    group_b = ChatRef("fake", "group-b")
    stores = memory_stores()
    stores.db = MemoryDb()
    platform = FakePlatform()
    jobs = JobsPlugin(
        JobsSettings(), AccessPolicy([group_a.key, group_b.key], []), platform, sender=AsyncMock()
    )
    config = MemoryJobSettings()
    jobs.service.repo = lambda _stores: config
    provider = ScriptedProvider(
        [
            Completion(Message.assistant("ok"), "end_turn"),
            Completion(Message.assistant("ok"), "end_turn"),
            Completion(Message.assistant("ok"), "end_turn"),
        ]
    )
    app, responder, _store = _memory_agent(stores, [jobs], provider)
    jobs._stores = stores
    await _agent_turn(app, responder, stores, message("remind me", chat=group_a))
    await _agent_turn(app, responder, stores, message("remind me", chat=group_b))
    assert "create_reminder" in {tool.name for tool in provider.calls[1]["tools"]}
    await config.configure(Scope(group_a.key, False), kind=JobKind.REMINDER, enabled=False)
    await _agent_turn(app, responder, stores, message("remind me", chat=group_b))
    assert "create_reminder" in {tool.name for tool in provider.calls[2]["tools"]}


@asynccontextmanager
async def _memory_skill_store():
    stores = memory_stores()
    yield None, stores.repo, ConversationStore()


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_malformed_newest_state_is_a_miss_on_both_backends(backend):
    """A malformed newest record must not fall back to an older valid one (backend parity)."""
    factory = _memory_skill_store if backend == "memory" else _postgres_skill_store
    async with factory() as (_db, repo, store):
        chat = ChatRef("test", f"parity-{uuid.uuid4().hex[:8]}")
        now = datetime.now(UTC)
        older = message("remind me", chat=chat)
        newer = message("and then", chat=chat)
        await repo.log_message(older)
        await repo.log_message(newer)
        requester = older.user.ref.key
        valid = {
            "requester": requester,
            "skills": [{"name": "reminders", "left": 2, "fresh_at": now.isoformat()}],
        }
        await store.add(chat, None, "assistant", {"skill_state": valid}, older.message_id)
        broken = {"requester": requester, "skills": None}
        await store.add(chat, None, "assistant", {"skill_state": broken}, newer.message_id)
        found = await store.latest_skill_state(
            chat,
            thread_id=None,
            conversation_id=None,
            requester=requester,
            since=now - timedelta(minutes=30),
            repo=repo,
        )
        assert found is None


async def test_memory_alternating_requesters_each_inherit_only_their_own_state():
    """Threading-disabled group: a tool-heavy turn by Bob doesn't touch Alice's state."""
    stores = memory_stores()
    store = ConversationStore()
    alice, bob = user("alice"), user("bob")
    now = datetime.now(UTC)

    async def turn(sender, skills, tool_rows):
        incoming = message("x", sender=sender)
        await stores.repo.log_message(incoming)
        for index in range(tool_rows):
            await store.add(
                GROUP,
                None,
                "assistant",
                {"text": "", "tool_calls": [{"id": f"c{index}", "name": "t", "arguments": {}}]},
                incoming.message_id,
            )
            await store.add(
                GROUP, None, "tool", {"tool_call_id": f"c{index}", "text": "r"}, incoming.message_id
            )
        state = {
            "requester": sender.ref.key,
            "skills": [{"name": name, "left": 2, "fresh_at": now.isoformat()} for name in skills],
        }
        await store.add(
            GROUP, None, "assistant", {"text": "ok", "skill_state": state}, incoming.message_id
        )

    async def read(sender):
        found = await store.latest_skill_state(
            GROUP,
            thread_id=None,
            conversation_id=None,
            requester=sender.ref.key,
            since=now - timedelta(minutes=30),
            repo=stores.repo,
        )
        return [item["name"] for item in found["skills"]] if found else None

    await turn(alice, ["reminders"], 0)
    await turn(bob, ["expenses"], 6)
    assert await read(alice) == ["reminders"]
    assert await read(bob) == ["expenses"]
    await turn(alice, ["polls"], 3)
    assert await read(alice) == ["polls"]
    assert await read(bob) == ["expenses"]


@pytest.mark.asyncio
async def test_real_jobs_store_toggle_crosses_topics_but_not_groups():
    """Real JobsRepo on Postgres: a reminders toggle committed while in topic A is seen by the
    next turn in topic B of the same group, and never by another group."""
    from chatcore_jobs.models import JobKind, Scope
    from chatcore_jobs.plugin import JobsPlugin
    from chatcore_jobs.repository import JobsRepo
    from chatcore_jobs.settings import JobsSettings

    if not TEST_PG_DSN:
        pytest.skip("TEST_PG_DSN is not configured")
    try:
        admin = await asyncpg.connect(TEST_PG_DSN, timeout=3)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres is unavailable: {type(exc).__name__}")
    schema = f"skill_jobs_{uuid.uuid4().hex[:10]}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        TEST_PG_DSN, min_size=1, max_size=2, timeout=3, server_settings={"search_path": schema}
    )
    db = PgDatabase(pool)
    try:
        group_b = ChatRef(GROUP.platform, "group-b")
        jobs = JobsPlugin(
            JobsSettings(),
            AccessPolicy([GROUP.key, group_b.key], []),
            FakePlatform(),
            sender=AsyncMock(),
        )
        await migrations.apply(
            db,
            [
                ("chatcore", resources.files("chatcore.migrations")),
                ("jobs", jobs.migrations()),
            ],
        )
        stores = Stores(repo=PgCoreRepo(db), cache=MemoryCache(prefix="t:"), db=db)
        jobs._stores = stores
        provider = ScriptedProvider(
            [Completion(Message.assistant("ok"), "end_turn") for _ in range(3)]
        )
        app, responder, _store = _memory_agent(stores, [jobs], provider)
        app.access = AccessPolicy([GROUP.key, group_b.key], [])

        def tools(index):
            return {tool.name for tool in provider.calls[index]["tools"]}

        repo = JobsRepo(db)
        await repo.configure(
            Scope(GROUP.key, False, thread_id="topic-a"), kind=JobKind.REMINDER, enabled=True
        )
        topic_b = message("remind me tomorrow", thread_id="topic-b")
        await _agent_turn(app, responder, stores, topic_b)
        assert "create_reminder" in tools(0)

        other_group = message("remind me tomorrow", chat=group_b)
        await _agent_turn(app, responder, stores, other_group)
        assert "create_reminder" not in tools(1)
        assert "list_reminders" in tools(1)

        await repo.configure(
            Scope(GROUP.key, False, thread_id="topic-a"), kind=JobKind.REMINDER, enabled=False
        )
        again = message("remind me later", sender=topic_b.user, thread_id="topic-b")
        await _agent_turn(app, responder, stores, again)
        assert "create_reminder" not in tools(2)
    finally:
        await db.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
