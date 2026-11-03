from typing import Any

from chatcore.display_time_store import (
    DisplayTimeService,
    PgDisplayTimeStore,
)
from chatcore.events import ChatRef, UserRef


class FakeDurableDb:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[str, str]] = {}

    async def fetchrow(self, sql: str, *args: Any):
        assert "chat_key = $1" in sql and "subject_key = $2" in sql
        row = self.rows.get((args[0], args[1]))
        return {"zone": row[0], "updated_by": row[1]} if row else None

    async def execute(self, sql: str, *args: Any) -> str:
        assert "chat_key" in sql
        chat = args[0]
        if sql.lstrip().startswith("INSERT"):
            subject = args[1]
            self.rows[(chat, subject)] = (args[2], args[3])
        elif "subject_key" not in sql:
            keys = [key for key in self.rows if key[0] == chat]
            for key in keys:
                del self.rows[key]
            return f"DELETE {len(keys)}"
        elif sql.lstrip().startswith("DELETE"):
            subject = args[1]
            self.rows.pop((chat, subject), None)
        return "OK"

    async def fetch(self, sql: str, *args: Any):
        return []


async def test_independent_services_share_persistent_preferences_and_keep_scopes_isolated() -> None:
    db = FakeDurableDb()
    alice = UserRef("tg", "42")
    group = ChatRef("tg", "-100")
    dm = ChatRef("tg", "42")
    first = DisplayTimeService(PgDisplayTimeStore(db))
    second = DisplayTimeService(PgDisplayTimeStore(db))

    await first.set_zone(group, alice, is_dm=False, zone="Asia/Singapore")
    await first.set_zone(dm, alice, is_dm=True, zone="Europe/London")

    assert await second.resolve(group, alice, is_dm=False) == ("Asia/Singapore", "group")
    assert await second.resolve(dm, alice, is_dm=True) == ("Europe/London", "personal")
    assert await second.resolve(ChatRef("tg", "-200"), alice, is_dm=False) == ("UTC", "fallback")


async def test_group_resolution_ignores_personal_preference_and_dm_must_be_self_owned() -> None:
    db = FakeDurableDb()
    service = DisplayTimeService(PgDisplayTimeStore(db))
    alice = UserRef("tg", "42")
    await service.set_zone(ChatRef("tg", "42"), alice, is_dm=True, zone="Europe/London")

    assert await service.resolve(ChatRef("tg", "-100"), alice, is_dm=False) == ("UTC", "fallback")
    try:
        await service.resolve(ChatRef("tg", "43"), alice, is_dm=True)
    except ValueError as exc:
        assert "own DM" in str(exc)
    else:
        raise AssertionError("a user must not read another DM's preference")


async def test_user_purge_only_removes_personal_preference() -> None:
    store = PgDisplayTimeStore(FakeDurableDb())
    service = DisplayTimeService(store)
    alice = UserRef("tg", "42")
    group = ChatRef("tg", "-100")
    dm = ChatRef("tg", "42")
    await service.set_zone(group, alice, is_dm=False, zone="Asia/Singapore")
    await service.set_zone(dm, alice, is_dm=True, zone="Europe/London")

    await store.purge_user(dm, alice)

    assert await service.resolve(dm, alice, is_dm=True) == ("UTC", "fallback")
    assert await service.resolve(group, alice, is_dm=False) == ("Asia/Singapore", "group")


async def test_chat_purge_removes_group_and_personal_rows_for_only_that_chat() -> None:
    db = FakeDurableDb()
    store = PgDisplayTimeStore(db)
    alice = UserRef("tg", "42")
    group = ChatRef("tg", "-100")
    dm = ChatRef("tg", "42")
    await store.set(group, "", "Asia/Singapore", alice)
    await store.set(dm, alice.key, "Europe/London", alice)

    assert await store.purge_chat(group) == 1
    assert await store.get(group, "") is None
    assert await store.get(dm, alice.key) is not None
