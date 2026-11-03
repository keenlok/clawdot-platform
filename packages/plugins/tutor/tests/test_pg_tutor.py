"""Postgres integration coverage for tutor-profile persistence."""

import os
import uuid
from importlib import resources

import asyncpg
import pytest
from chatcore.store.pg import PgDatabase
from chatcore_tutor.repository import TutorRepo

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_PG_DSN", "postgresql://chatcore:chatcore@127.0.0.1:55432/chatcore_test")


@pytest.fixture
async def db():
    schema = f"tutor_{uuid.uuid4().hex[:8]}"
    try:
        pool = await asyncpg.create_pool(
            DSN, min_size=1, max_size=2, timeout=3, server_settings={"search_path": schema}
        )
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"Postgres not available: {type(exc).__name__}")
    database = PgDatabase(pool)
    await database.execute(f'CREATE SCHEMA "{schema}"')
    try:
        for name in ("0001_tutor_profiles.sql", "0002_tutor_topics.sql"):
            migration = resources.files("chatcore_tutor.migrations").joinpath(name)
            await database.execute(migration.read_text("utf-8"))
        yield database
    finally:
        await database.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await database.close()


async def test_tutor_profile_round_trip_and_database_checks(db):
    repo = TutorRepo(db)
    profile = await repo.upsert("tg:1", "Japanese", "A2", "English")
    assert profile.target == "Japanese" and profile.active
    assert (await repo.get("tg:1")) == profile
    bilingual = await repo.upsert("tg:2", "Cantonese", "B1", ("English", "Mandarin Chinese"))
    assert bilingual.explain == ("English", "Mandarin Chinese")
    assert await repo.get("tg:2") == bilingual
    assert await repo.set_active("tg:1", False)
    assert not (await repo.get("tg:1")).active
    assert not await repo.set_active("tg:missing", True)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO tutor_profiles (user_key,target,level,explain) "
            "VALUES ('tg:bad-level','Japanese','Z9','English')"
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO tutor_profiles (user_key,target,level,explain) "
            "VALUES ('tg:same','Japanese','A2','Japanese')"
        )


async def test_invalid_stored_profile_is_missing_and_delete_reports_result(db):
    repo = TutorRepo(db)
    await db.execute(
        "INSERT INTO tutor_profiles (user_key,target,level,explain) "
        "VALUES ('tg:legacy','Unlisted','A2','English')"
    )
    assert await repo.get("tg:legacy") is None
    assert await repo.delete("tg:legacy")
    assert not await repo.delete("tg:legacy")


async def test_legacy_single_explanation_and_invalid_joined_value(db):
    repo = TutorRepo(db)
    await db.execute(
        "INSERT INTO tutor_profiles (user_key,target,level,explain) "
        "VALUES ('tg:legacy-single','Japanese','A2','English')"
    )
    assert (await repo.get("tg:legacy-single")).explain == ("English",)
    await db.execute(
        "INSERT INTO tutor_profiles (user_key,target,level,explain) "
        "VALUES ('tg:invalid-pair','Japanese','A2','English,Klingon')"
    )
    assert await repo.get("tg:invalid-pair") is None


async def test_tutor_topics_are_scoped_by_chat_and_thread_and_enforce_checks(db):
    repo = TutorRepo(db)
    topic = await repo.upsert_topic("tg:-100", "7", "ja", "a2", "en", "tg:admin")
    assert topic.target == "Japanese" and topic.level == "A2" and topic.explain == ("English",)
    assert not topic.auto_reply
    assert await repo.get_topic("tg:-100", "7") == topic
    bilingual_topic = await repo.upsert_topic(
        "tg:-100", "9", "Cantonese", "A1", ("English", "Mandarin Chinese"), "tg:admin", True
    )
    assert bilingual_topic.explain == ("English", "Mandarin Chinese")
    assert bilingual_topic.auto_reply
    assert await repo.get_topic("tg:-100", "9") == bilingual_topic
    assert await repo.set_topic_auto_reply("tg:-100", "7", True)
    updated = await repo.upsert_topic("tg:-100", "7", "French", "B1", None, "tg:admin")
    assert updated.auto_reply
    assert await repo.set_topic_auto_reply("tg:-100", "7", False)
    assert not (await repo.get_topic("tg:-100", "7")).auto_reply
    assert not await repo.set_topic_auto_reply("tg:-missing", "7", True)
    assert await repo.get_topic("tg:-100", "8") is None
    assert await repo.get_topic("tg:-100", "") is None
    await repo.upsert_topic("tg:-100", "", None, None, None, "tg:admin")
    assert await repo.get_topic("tg:-100", "") is not None
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            "INSERT INTO tutor_topics (chat_key,thread_key,set_by) "
            "VALUES ('tg:-100','7','tg:admin')"
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO tutor_topics (chat_key,thread_key,target,level,set_by) "
            "VALUES ('tg:-100','bad','Japanese','Z9','tg:admin')"
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO tutor_topics (chat_key,thread_key,target,level,explain,set_by) "
            "VALUES ('tg:-100','same','Japanese','A2','Japanese','tg:admin')"
        )
    await repo.upsert_topic("tg:-101", "7", None, None, None, "tg:admin")
    await repo.purge_chat("tg:-100")
    assert await repo.get_topic("tg:-100", "7") is None
    assert await repo.get_topic("tg:-100", "") is None
    assert await repo.get_topic("tg:-101", "7") is not None


async def test_invalid_stored_topic_is_unmarked(db):
    repo = TutorRepo(db)
    await db.execute(
        "INSERT INTO tutor_topics (chat_key,thread_key,target,level,set_by) "
        "VALUES ('tg:-100','7','Unsupported','A2','tg:admin')"
    )
    assert await repo.get_topic("tg:-100", "7") is None
    await db.execute(
        "INSERT INTO tutor_topics (chat_key,thread_key,target,level,explain,set_by) "
        "VALUES ('tg:-100','invalid-pair','Japanese','A2','English,Klingon','tg:admin')"
    )
    assert await repo.get_topic("tg:-100", "invalid-pair") is None
