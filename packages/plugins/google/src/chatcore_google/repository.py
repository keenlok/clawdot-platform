"""Scoped opt-outs, durable remote-write reservations and itinerary metadata."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4


class GoogleRepository:
    """Memory fallback for console/tests; deployed apps inject the Postgres database."""

    def __init__(self, db=None):
        self.db = db
        self._settings = {}
        self._actions = {}
        self._links = {}
        self._files = {}
        self._lock = asyncio.Lock()

    async def settings(self, chat):
        if self.db is not None:
            row = await self.db.fetchrow(
                "SELECT enabled,revision FROM google_settings WHERE chat_key=$1", chat
            )
            return (row["enabled"], row["revision"]) if row else (True, 0)
        return self._settings.get(chat, (True, 0))

    async def configure(self, chat, enabled, expected_revision):
        if type(enabled) is not bool:
            raise ValueError("Specify on or off.")
        if self.db is not None:
            async with self.db.transaction() as tx:
                await tx.execute("SELECT pg_advisory_xact_lock(hashtext('google:' || $1))", chat)
                row = await tx.fetchrow(
                    "SELECT revision FROM google_settings WHERE chat_key=$1", chat
                )
                if (row["revision"] if row else 0) != expected_revision:
                    raise ValueError("Google settings changed; request the change again.")
                await tx.execute(
                    "INSERT INTO google_settings(chat_key,enabled,revision) VALUES($1,$2,1) "
                    "ON CONFLICT(chat_key) DO UPDATE "
                    "SET enabled=$2,revision=google_settings.revision+1",
                    chat,
                    enabled,
                )
        else:
            async with self._lock:
                if (await self.settings(chat))[1] != expected_revision:
                    raise ValueError("Google settings changed; request the change again.")
                self._settings[chat] = (enabled, expected_revision + 1)

    async def reserve(self, chat, action_id, requester, rows, daily_limit, revision):
        """Commit the reservation BEFORE any remote write, so a crash cannot authorize replay."""
        now = datetime.now(UTC)
        if self.db is not None:
            async with self.db.transaction() as tx:
                await tx.execute("SELECT pg_advisory_xact_lock(hashtext('google:' || $1))", chat)
                row = await tx.fetchrow(
                    "SELECT enabled,revision FROM google_settings WHERE chat_key=$1", chat
                )
                if (
                    row
                    and (not row["enabled"] or row["revision"] != revision)
                    or not row
                    and revision
                ):
                    raise ValueError("Google settings changed or are off; request the write again.")
                existing = await tx.fetchval(
                    "SELECT 1 FROM google_actions WHERE chat_key=$1 AND action_id=$2",
                    chat,
                    action_id,
                )
                if existing:
                    raise ValueError(
                        "This action was handled or its result is uncertain; check the file."
                    )
                used = await tx.fetchval(
                    "SELECT COALESCE(sum(suggestion_rows),0) FROM google_actions "
                    "WHERE chat_key=$1 AND created_at >= $2",
                    chat,
                    now.replace(hour=0, minute=0, second=0, microsecond=0),
                )
                if rows and used + rows > daily_limit:
                    raise ValueError("This chat has reached today's suggestion-row limit (UTC).")
                await tx.execute(
                    "INSERT INTO google_actions"
                    "(chat_key,action_id,requester_key,suggestion_rows,created_at) "
                    "VALUES($1,$2,$3,$4,$5)",
                    chat,
                    action_id,
                    requester,
                    rows,
                    now,
                )
        else:
            async with self._lock:
                enabled, current = await self.settings(chat)
                if not enabled or current != revision:
                    raise ValueError("Google settings changed or are off; request the write again.")
                key = (chat, action_id)
                if key in self._actions:
                    raise ValueError(
                        "This action was handled or its result is uncertain; check the file."
                    )
                used = sum(
                    value[1]
                    for (scope, _), value in self._actions.items()
                    if scope == chat and value[0].date() == now.date()
                )
                if rows and used + rows > daily_limit:
                    raise ValueError("This chat has reached today's suggestion-row limit (UTC).")
                self._actions[key] = (now, rows, requester)

    async def trip_files(self, chat, trip_id):
        if self.db is not None:
            rows = await self.db.fetch(
                "SELECT file_id,trip_id,url,name,mime,role,owner,requester,version "
                "FROM google_trip_files WHERE chat_key=$1 AND trip_id=$2 ORDER BY file_id",
                chat,
                trip_id,
            )
            return [dict(row) for row in rows]
        return [
            dict(value)
            for (scope, _), value in sorted(self._files.items())
            if scope == chat and value["trip_id"] == trip_id
        ]

    async def trip_file(self, chat, file_id):
        if self.db is not None:
            row = await self.db.fetchrow(
                "SELECT file_id,trip_id,url,name,mime,role,owner,requester,version "
                "FROM google_trip_files WHERE chat_key=$1 AND file_id=$2",
                chat,
                file_id,
            )
            return dict(row) if row else None
        value = self._files.get((chat, file_id))
        return dict(value) if value else None

    @staticmethod
    def file_state(rows):
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

    async def apply_file(self, chat, trip_id, file_id, data, expected, *, revision=None):
        """CAS local associations and the legacy Sheet link in one transaction."""
        if self.db is not None:
            async with self.db.transaction() as tx:
                await tx.execute("SELECT pg_advisory_xact_lock(hashtext('google:' || $1))", chat)
                repo = GoogleRepository(tx)
                await repo._apply_file(chat, trip_id, file_id, data, expected, revision)
        else:
            async with self._lock:
                await self._apply_file(chat, trip_id, file_id, data, expected, revision)

    async def _apply_file(self, chat, trip_id, file_id, data, expected, revision):
        if revision is not None:
            enabled, current_revision = await self.settings(chat)
            if not enabled or revision != current_revision:
                raise ValueError("Google settings changed; request a fresh confirmation.")
        if self.file_state(await self.trip_files(chat, trip_id)) != expected:
            raise ValueError("Trip files changed; request a fresh confirmation.")
        current = await self.trip_file(chat, file_id)
        if current and current["trip_id"] != trip_id:
            raise ValueError("This file belongs to another trip; remove that association first.")
        if data is None and current is None:
            raise ValueError("This file is no longer attached to the trip.")
        if data is not None and data["role"] not in {"itinerary", "reference"}:
            raise ValueError("Choose itinerary or reference.")
        if self.db is not None:
            if data is None:
                await self.db.execute(
                    "DELETE FROM google_trip_files WHERE chat_key=$1 AND file_id=$2", chat, file_id
                )
            else:
                if data["role"] == "itinerary":
                    await self.db.execute(
                        "UPDATE google_trip_files SET role='reference',version=$3,updated_at=now() "
                        "WHERE chat_key=$1 AND trip_id=$2 AND role='itinerary' AND file_id<>$4",
                        chat,
                        trip_id,
                        uuid4().hex,
                        file_id,
                    )
                await self.db.execute(
                    "INSERT INTO google_trip_files(chat_key,file_id,trip_id,url,name,"
                    "mime,role,owner,requester,version) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT(chat_key,file"
                    "_id) DO UPDATE "
                    "SET trip_id=$3,url=$4,name=$5,mime=$6,role=$7,owner=$8,requester"
                    "=$9,version=$10,updated_at=now()",
                    chat,
                    file_id,
                    trip_id,
                    data["url"],
                    data["name"],
                    data["mime"],
                    data["role"],
                    data["owner"],
                    data["requester"],
                    uuid4().hex,
                )
            if data is not None and data["mime"] == "application/vnd.google-apps.spreadsheet":
                await self.db.execute(
                    "INSERT INTO google_itinerary_links(chat_key,file_id,trip_id,requ"
                    "ester_key) VALUES($1,$2,$3,$4) "
                    "ON CONFLICT(chat_key,file_id) DO UPDATE SET trip_id=$3,requester"
                    "_key=$4,updated_at=now()",
                    chat,
                    file_id,
                    trip_id,
                    data["requester"],
                )
            else:
                await self.db.execute(
                    "DELETE FROM google_itinerary_links WHERE chat_key=$1 AND file_id=$2",
                    chat,
                    file_id,
                )
        else:
            if data is None:
                self._files.pop((chat, file_id), None)
            else:
                if data["role"] == "itinerary":
                    for (scope, fid), value in self._files.items():
                        if (
                            scope == chat
                            and value["trip_id"] == trip_id
                            and value["role"] == "itinerary"
                            and fid != file_id
                        ):
                            value.update(role="reference", version=uuid4().hex)
                self._files[(chat, file_id)] = dict(
                    data, file_id=file_id, trip_id=trip_id, version=uuid4().hex
                )
            if data is not None and data["mime"] == "application/vnd.google-apps.spreadsheet":
                self._links[(chat, file_id)] = (trip_id, data["requester"])
            else:
                self._links.pop((chat, file_id), None)

    async def link(self, chat, file_id, trip_id, requester):
        current = await self.trip_file(chat, file_id)
        data = current or {
            "url": "https://docs.google.com/spreadsheets/d/" + file_id + "/edit",
            "name": "Legacy linked spreadsheet",
            "mime": "application/vnd.google-apps.spreadsheet",
            "role": "reference",
            "owner": None,
        }
        data = {key: data[key] for key in ("url", "name", "mime", "role", "owner")}
        data["requester"] = requester
        await self.apply_file(
            chat, trip_id, file_id, data, self.file_state(await self.trip_files(chat, trip_id))
        )

    async def linked_trip(self, chat, file_id):
        if self.db is not None:
            return await self.db.fetchval(
                "SELECT trip_id FROM google_itinerary_links WHERE chat_key=$1 AND file_id=$2",
                chat,
                file_id,
            )
        value = self._links.get((chat, file_id))
        return value[0] if value else None

    async def purge(self, chat, user=None):
        if self.db is not None:
            await self.db.execute(
                "DELETE FROM google_actions WHERE chat_key=$1 "
                "AND ($2::text IS NULL OR requester_key=$2)",
                chat,
                user,
            )
            await self.db.execute(
                "DELETE FROM google_itinerary_links WHERE chat_key=$1 "
                "AND ($2::text IS NULL OR requester_key=$2)",
                chat,
                user,
            )
            await self.db.execute(
                "DELETE FROM google_trip_files WHERE chat_key=$1 AND ($2::text IS"
                " NULL OR requester=$2)",
                chat,
                user,
            )
            if user is None:
                await self.db.execute("DELETE FROM google_settings WHERE chat_key=$1", chat)
        else:
            self._actions = {
                k: v
                for k, v in self._actions.items()
                if k[0] != chat or user is not None and v[2] != user
            }
            self._links = {
                k: v
                for k, v in self._links.items()
                if k[0] != chat or user is not None and v[1] != user
            }
            self._files = {
                key: value
                for key, value in self._files.items()
                if key[0] != chat or user is not None and value["requester"] != user
            }
            if user is None:
                self._settings.pop(chat, None)

    async def purge_older_than(self, cutoff):
        if self.db is not None:
            await self.db.execute("DELETE FROM google_actions WHERE created_at<$1", cutoff)
        else:
            self._actions = {k: v for k, v in self._actions.items() if v[0] >= cutoff}
