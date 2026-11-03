"""Scoped persistence and resolution for display-time preferences."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from chatcore.display_time import validate_zone
from chatcore.events import ChatRef, UserRef
from chatcore.store.base import Database, Stores


@dataclass(frozen=True, slots=True)
class DisplayTimePreference:
    zone: str
    updated_by: str


class DisplayTimeStore(Protocol):
    async def get(self, chat: ChatRef, subject: str) -> DisplayTimePreference | None: ...
    async def set(self, chat: ChatRef, subject: str, zone: str, updated_by: UserRef) -> None: ...
    async def delete(self, chat: ChatRef, subject: str) -> None: ...
    async def purge_user(self, chat: ChatRef, user: UserRef) -> int: ...
    async def purge_chat(self, chat: ChatRef) -> int: ...


class MemoryDisplayTimeStore:
    """Development store owned by one Stores object and lost with the process."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], DisplayTimePreference] = {}

    async def get(self, chat: ChatRef, subject: str) -> DisplayTimePreference | None:
        return self._rows.get((chat.key, subject))

    async def set(self, chat: ChatRef, subject: str, zone: str, updated_by: UserRef) -> None:
        self._rows[(chat.key, subject)] = DisplayTimePreference(zone, updated_by.key)

    async def delete(self, chat: ChatRef, subject: str) -> None:
        self._rows.pop((chat.key, subject), None)

    async def purge_user(self, chat: ChatRef, user: UserRef) -> int:
        return int(self._rows.pop((chat.key, user.key), None) is not None)

    async def purge_chat(self, chat: ChatRef) -> int:
        keys = [key for key in self._rows if key[0] == chat.key]
        for key in keys:
            del self._rows[key]
        return len(keys)


class PgDisplayTimeStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, chat: ChatRef, subject: str) -> DisplayTimePreference | None:
        row = await self.db.fetchrow(
            "SELECT zone, updated_by FROM display_time_preferences "
            "WHERE chat_key = $1 AND subject_key = $2",
            chat.key,
            subject,
        )
        if row is None:
            return None
        return DisplayTimePreference(row["zone"], row["updated_by"])

    async def set(self, chat: ChatRef, subject: str, zone: str, updated_by: UserRef) -> None:
        await self.db.execute(
            """INSERT INTO display_time_preferences (chat_key, subject_key, zone, updated_by)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (chat_key, subject_key) DO UPDATE
               SET zone = EXCLUDED.zone, updated_by = EXCLUDED.updated_by, updated_at = now()""",
            chat.key,
            subject,
            zone,
            updated_by.key,
        )

    async def delete(self, chat: ChatRef, subject: str) -> None:
        await self.db.execute(
            "DELETE FROM display_time_preferences WHERE chat_key = $1 AND subject_key = $2",
            chat.key,
            subject,
        )

    async def purge_user(self, chat: ChatRef, user: UserRef) -> int:
        status = await self.db.execute(
            "DELETE FROM display_time_preferences WHERE chat_key = $1 AND subject_key = $2",
            chat.key,
            user.key,
        )
        return _affected(status)

    async def purge_chat(self, chat: ChatRef) -> int:
        status = await self.db.execute(
            "DELETE FROM display_time_preferences WHERE chat_key = $1", chat.key
        )
        return _affected(status)


def _affected(status: str) -> int:
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


class DisplayTimeService:
    def __init__(self, store: DisplayTimeStore) -> None:
        self.store = store

    async def resolve(self, chat: ChatRef, user: UserRef, *, is_dm: bool) -> tuple[str, str]:
        if is_dm:
            self._validate_dm_scope(chat, user)
            preference = await self.store.get(chat, user.key)
            return (preference.zone, "personal") if preference else ("UTC", "fallback")
        preference = await self.store.get(chat, "")
        return (preference.zone, "group") if preference else ("UTC", "fallback")

    async def set_zone(
        self, chat: ChatRef, user: UserRef, *, is_dm: bool, zone: str
    ) -> tuple[str, str]:
        name = validate_zone(zone)
        subject = user.key if is_dm else ""
        if is_dm:
            self._validate_dm_scope(chat, user)
        await self.store.set(chat, subject, name, user)
        return name, "personal" if is_dm else "group"

    async def reset(self, chat: ChatRef, user: UserRef, *, is_dm: bool) -> tuple[str, str]:
        subject = user.key if is_dm else ""
        if is_dm:
            self._validate_dm_scope(chat, user)
        await self.store.delete(chat, subject)
        return await self.resolve(chat, user, is_dm=is_dm)

    async def purge_user(self, chat: ChatRef, user: UserRef) -> int:
        return await self.store.purge_user(chat, user)

    async def purge_chat(self, chat: ChatRef) -> int:
        return await self.store.purge_chat(chat)

    @staticmethod
    def _validate_dm_scope(chat: ChatRef, user: UserRef) -> None:
        if chat.platform != user.platform or chat.id != user.id:
            raise ValueError("personal display settings are available only in your own DM")


def display_service(stores: Stores) -> DisplayTimeService:
    """Return the per-Stores service without adding mutable state to shared contracts."""
    service = stores.extra.get("display_time_service")
    if service is None:
        store: DisplayTimeStore = (
            PgDisplayTimeStore(stores.db) if stores.db is not None else MemoryDisplayTimeStore()
        )
        service = DisplayTimeService(store)
        stores.extra["display_time_service"] = service
    return service
