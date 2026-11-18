"""Scoped opt-outs, durable remote-write reservations and itinerary metadata."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4


class GoogleRepository:
    """Memory fallback for console/tests; deployed apps inject the Postgres database."""

    def __init__(self, db=None, *, unresolved_retention_days=90):
        self.db = db
        self.unresolved_retention_days = unresolved_retention_days
        self._settings = {}
        self._actions = {}
        self._edit_results = {}
        self._links = {}
        self._files = {}
        self._outputs = {}
        self._create_reservations = {}
        self._confirm_writes = {}
        self._lock = asyncio.Lock()

    async def settings(self, chat):
        if self.db is not None:
            row = await self.db.fetchrow(
                "SELECT enabled,revision FROM google_settings WHERE chat_key=$1", chat
            )
            return (row["enabled"], row["revision"]) if row else (True, 0)
        return self._settings.get(chat, (True, 0))

    async def configure(self, chat, enabled, expected_revision):
        await self.configure_fields(chat, enabled=enabled, expected_revision=expected_revision)

    async def configure_fields(self, chat, *, enabled=None, confirm_writes=None, expected_revision):
        if enabled is None and confirm_writes is None:
            raise ValueError("Specify a Google setting to change.")
        if (enabled is not None and type(enabled) is not bool) or (
            confirm_writes is not None and type(confirm_writes) is not bool
        ):
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
                    "INSERT INTO google_settings(chat_key,enabled,confirm_writes,revision) "
                    "VALUES($1,COALESCE($2,TRUE),COALESCE($3,TRUE),1) ON CONFLICT(chat_key) "
                    "DO UPDATE SET enabled=COALESCE($2,google_settings.enabled), "
                    "confirm_writes=COALESCE($3,google_settings.confirm_writes), "
                    "revision=google_settings.revision+1",
                    chat,
                    enabled,
                    confirm_writes,
                )
        else:
            async with self._lock:
                if (await self.settings(chat))[1] != expected_revision:
                    raise ValueError("Google settings changed; request the change again.")
                old_enabled = self._settings.get(chat, (True, 0))[0]
                self._settings[chat] = (
                    old_enabled if enabled is None else enabled,
                    expected_revision + 1,
                )
                if confirm_writes is not None:
                    self._confirm_writes[chat] = confirm_writes

    async def confirm_writes(self, chat):
        if self.db is not None:
            value = await self.db.fetchval(
                "SELECT confirm_writes FROM google_settings WHERE chat_key=$1", chat
            )
            return True if value is None else value
        return self._confirm_writes.get(chat, True)

    async def record_output(
        self, chat, file_id, mime, requester_key, thread_id, owner_id, action_id
    ):
        if self.db is not None:
            await self.db.execute(
                "INSERT INTO google_outputs(chat_key,file_id,mime,requester_key,thread_id,"
                "owner_id,action_id) "
                "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(chat_key,file_id) DO NOTHING",
                chat,
                file_id,
                mime,
                requester_key,
                thread_id,
                owner_id,
                action_id,
            )
        else:
            self._outputs.setdefault(
                (chat, file_id),
                dict(
                    chat_key=chat,
                    file_id=file_id,
                    mime=mime,
                    requester_key=requester_key,
                    thread_id=thread_id,
                    owner_id=owner_id,
                    action_id=action_id,
                    created_at=datetime.now(UTC),
                    trashed_at=None,
                ),
            )

    async def output(self, chat, thread_id, file_id):
        if self.db is not None:
            row = await self.db.fetchrow(
                "SELECT * FROM google_outputs WHERE chat_key=$1 "
                "AND thread_id IS NOT DISTINCT FROM $2 "
                "AND file_id=$3 AND trashed_at IS NULL",
                chat,
                thread_id,
                file_id,
            )
            return dict(row) if row else None
        row = self._outputs.get((chat, file_id))
        return row if row and row["thread_id"] == thread_id and row["trashed_at"] is None else None

    async def creates_today(self, chat, requester_key):
        since = datetime.now(UTC) - timedelta(hours=24)
        if self.db is not None:
            return await self.db.fetchval(
                "SELECT count(*) FROM google_create_reservations WHERE chat_key=$1 "
                "AND requester_key=$2 AND created_at >= $3",
                chat,
                requester_key,
                since,
            )
        return sum(
            1
            for (scope, _), row in self._create_reservations.items()
            if scope == chat
            and row["requester_key"] == requester_key
            and row["created_at"] >= since
        )

    async def reserve_create(self, chat, action_id, requester_key, limit):
        """Atomically reserve one of a member's daily create slots for this action."""
        since = datetime.now(UTC) - timedelta(hours=24)
        message = (
            f"You've reached today's limit of {limit} new Google files; "
            "try again tomorrow or ask a bot admin."
        )
        if self.db is not None:
            async with self.db.transaction() as tx:
                await tx.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1 || ':' || $2))",
                    chat,
                    requester_key,
                )
                existing = await tx.fetchrow(
                    "SELECT requester_key FROM google_create_reservations "
                    "WHERE chat_key=$1 AND action_id=$2",
                    chat,
                    action_id,
                )
                if existing:
                    if existing["requester_key"] != requester_key:
                        raise PermissionError(
                            "This create reservation belongs to another requester."
                        )
                    return
                used = await tx.fetchval(
                    "SELECT count(*) FROM google_create_reservations "
                    "WHERE chat_key=$1 AND requester_key=$2 AND created_at >= $3",
                    chat,
                    requester_key,
                    since,
                )
                if used >= limit:
                    raise PermissionError(message)
                await tx.execute(
                    "INSERT INTO google_create_reservations(chat_key,action_id,requester_key) "
                    "VALUES($1,$2,$3)",
                    chat,
                    action_id,
                    requester_key,
                )
            return
        async with self._lock:
            key = (chat, action_id)
            existing = self._create_reservations.get(key)
            if existing is not None:
                if existing["requester_key"] != requester_key:
                    raise PermissionError("This create reservation belongs to another requester.")
                return
            used = sum(
                1
                for (scope, _), row in self._create_reservations.items()
                if scope == chat
                and row["requester_key"] == requester_key
                and row["created_at"] >= since
            )
            if used >= limit:
                raise PermissionError(message)
            self._create_reservations[key] = {
                "chat_key": chat,
                "action_id": action_id,
                "requester_key": requester_key,
                "created_at": datetime.now(UTC),
            }

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

    async def reserve_edit(
        self,
        chat,
        action_id,
        requester,
        rows,
        daily_limit,
        revision,
        *,
        kind,
        source_id,
        output_binding=None,
    ):
        """Atomically reserve replay protection and the edit result journal."""
        if kind not in {"edit_doc", "edit_sheet", "browser_edit_doc", "create_doc"}:
            raise ValueError("Unknown Google edit action kind.")
        if kind == "create_doc":
            if source_id is not None:
                raise ValueError("A new Doc reservation cannot have a source ID.")
        elif not isinstance(source_id, str) or not 1 <= len(source_id) <= 200:
            raise ValueError("A valid Google source ID is required.")
        if output_binding is not None:
            if not isinstance(output_binding, dict):
                raise ValueError("Output authority binding is invalid.")
            owner_id = output_binding.get("owner_id")
            owner_email = output_binding.get("owner_email")
            thread_id = output_binding.get("thread_id")
            output_mode = output_binding.get("mode")
            valid_common = (
                isinstance(owner_id, str)
                and 1 <= len(owner_id) <= 200
                and isinstance(owner_email, str)
                and 1 <= len(owner_email) <= 320
                and (thread_id is None or isinstance(thread_id, str))
            )
            valid_mode = (
                kind == "create_doc" and source_id is None and output_mode == "create"
            ) or (
                kind == "edit_doc"
                and source_id is not None
                and output_mode in {"copy", "docx_import"}
            )
            if not valid_common or not valid_mode:
                raise ValueError("Output authority binding does not match this new Doc action.")
        now = datetime.now(UTC)
        if self.db is not None:
            async with self.db.transaction() as tx:
                await tx.execute("SELECT pg_advisory_xact_lock(hashtext('google:' || $1))", chat)
                row = await tx.fetchrow(
                    "SELECT enabled,revision FROM google_settings WHERE chat_key=$1", chat
                )
                if (row and (not row["enabled"] or row["revision"] != revision)) or (
                    not row and revision
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
                await tx.execute(
                    "INSERT INTO google_edit_results(chat_key,action_id,kind,phase,source_id,"
                    "output_owner_id,output_owner_email,output_thread_id,output_mode) "
                    "VALUES($1,$2,$3,'reserved',$4,$5,$6,$7,$8)",
                    chat,
                    action_id,
                    kind,
                    source_id,
                    output_binding.get("owner_id") if output_binding else None,
                    output_binding.get("owner_email") if output_binding else None,
                    output_binding.get("thread_id") if output_binding else None,
                    output_binding.get("mode") if output_binding else None,
                )
            return
        async with self._lock:
            enabled, current = self._settings.get(chat, (True, 0))
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
            self._edit_results[key] = {
                "chat_key": chat,
                "action_id": action_id,
                "kind": kind,
                "phase": "reserved",
                "source_id": source_id,
                "destination_id": None,
                "destination_creation_claimed": False,
                "output_owner_id": output_binding.get("owner_id") if output_binding else None,
                "output_owner_email": output_binding.get("owner_email") if output_binding else None,
                "output_thread_id": output_binding.get("thread_id") if output_binding else None,
                "output_mode": output_binding.get("mode") if output_binding else None,
                "failed_stage": None,
                "error_category": None,
                "created_at": now,
                "updated_at": now,
            }

    async def claim_destination_creation(self, chat, action_id, *, kind, source_id, revision):
        """Durably claim the single remote copy/import attempt for a reserved edit."""
        if self.db is not None:
            async with self.db.transaction() as tx:
                await tx.execute("SELECT pg_advisory_xact_lock(hashtext('google:' || $1))", chat)
                settings = await tx.fetchrow(
                    "SELECT enabled,revision FROM google_settings WHERE chat_key=$1", chat
                )
                enabled, current_revision = (
                    (settings["enabled"], settings["revision"]) if settings else (True, 0)
                )
                if not enabled or current_revision != revision:
                    raise ValueError("Google settings changed or are off; request the write again.")
                claimed = await tx.fetchval(
                    "UPDATE google_edit_results SET destination_creation_claimed=TRUE,"
                    "updated_at=now() WHERE chat_key=$1 AND action_id=$2 AND kind=$3 "
                    "AND source_id IS NOT DISTINCT FROM $4 AND phase='reserved' "
                    "AND destination_creation_claimed=FALSE RETURNING TRUE",
                    chat,
                    action_id,
                    kind,
                    source_id,
                )
                if not claimed:
                    raise ValueError(
                        "Destination creation was already claimed or this reservation changed."
                    )
            return True
        async with self._lock:
            enabled, current_revision = self._settings.get(chat, (True, 0))
            if not enabled or current_revision != revision:
                raise ValueError("Google settings changed or are off; request the write again.")
            result = self._edit_results.get((chat, action_id))
            if (
                result is None
                or result["kind"] != kind
                or result["source_id"] != source_id
                or result["phase"] != "reserved"
                or result["destination_creation_claimed"]
            ):
                raise ValueError(
                    "Destination creation was already claimed or this reservation changed."
                )
            result["destination_creation_claimed"] = True
            result["updated_at"] = datetime.now(UTC)
        return True

    async def edit_result(self, chat, action_id):
        if self.db is not None:
            row = await self.db.fetchrow(
                "SELECT chat_key,action_id,kind,phase,source_id,destination_id,failed_stage,"
                "destination_creation_claimed,error_category,created_at,updated_at,"
                "output_owner_id,output_owner_email,output_thread_id,output_mode "
                "FROM google_edit_results "
                "WHERE chat_key=$1 AND action_id=$2",
                chat,
                action_id,
            )
            return dict(row) if row else None
        result = self._edit_results.get((chat, action_id))
        return dict(result) if result else None

    async def output_destination(self, chat, thread_id, destination_id):
        """Find one retained, exact-topic binding for a known newly-created Doc destination."""
        if (
            not isinstance(destination_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", destination_id)
            or (thread_id is not None and not isinstance(thread_id, str))
        ):
            return None
        if self.db is not None:
            rows = await self.db.fetch(
                "SELECT e.chat_key,e.action_id,e.kind,e.phase,e.source_id,e.destination_id,"
                "e.output_owner_id,e.output_owner_email,e.output_thread_id,e.output_mode "
                "FROM google_edit_results e WHERE e.chat_key=$1 "
                "AND e.output_thread_id IS NOT DISTINCT FROM $2 AND e.destination_id=$3 "
                "AND e.phase IN ('destination_created','applying','shared','verified','partial',"
                "'uncertain') AND e.output_owner_id IS NOT NULL "
                "AND e.output_owner_email IS NOT NULL AND e.output_mode IS NOT NULL "
                "AND ((e.kind='create_doc' AND e.source_id IS NULL AND e.output_mode='create') "
                "OR (e.kind='edit_doc' AND e.source_id IS NOT NULL "
                "AND e.source_id<>e.destination_id AND e.output_mode IN ('copy','docx_import'))) "
                "LIMIT 2",
                chat,
                thread_id,
                destination_id,
            )
            return dict(rows[0]) if len(rows) == 1 else None
        rows = [
            result
            for (scope, _), result in self._edit_results.items()
            if scope == chat
            and result.get("output_thread_id") == thread_id
            and result.get("destination_id") == destination_id
            and result.get("phase")
            in {"destination_created", "applying", "shared", "verified", "partial", "uncertain"}
            and result.get("output_owner_id")
            and result.get("output_owner_email")
            and (
                result.get("kind") == "create_doc"
                and result.get("source_id") is None
                and result.get("output_mode") == "create"
                or result.get("kind") == "edit_doc"
                and result.get("source_id")
                and result.get("source_id") != destination_id
                and result.get("output_mode") in {"copy", "docx_import"}
            )
        ]
        return dict(rows[0]) if len(rows) == 1 else None

    async def browser_edit_results(self, chat, requester=None):
        """List browser-edit results scoped to a chat and, optionally, requester key."""
        if self.db is not None:
            rows = await self.db.fetch(
                "SELECT e.action_id,e.phase,e.source_id,e.destination_id,e.failed_stage,"
                "e.error_category,e.created_at,e.updated_at FROM google_edit_results e "
                "JOIN google_actions a USING(chat_key,action_id) "
                "WHERE e.chat_key=$1 AND e.kind='browser_edit_doc' "
                "AND ($2::text IS NULL OR a.requester_key=$2) ORDER BY e.created_at,e.action_id",
                chat,
                requester,
            )
            return [dict(row) for row in rows]
        return [
            {
                key: value[key]
                for key in (
                    "action_id",
                    "phase",
                    "source_id",
                    "destination_id",
                    "failed_stage",
                    "error_category",
                    "created_at",
                    "updated_at",
                )
            }
            for (scope, _), value in sorted(self._edit_results.items())
            if scope == chat
            and value["kind"] == "browser_edit_doc"
            and (requester is None or self._actions[(scope, value["action_id"])][2] == requester)
        ]

    async def update_edit_result(
        self, chat, action_id, *, phase, destination_id=None, failed_stage=None, error_category=None
    ):
        phases = {
            "reserved",
            "destination_created",
            "shared",
            "applying",
            "verified",
            "conflict",
            "partial",
            "uncertain",
        }
        if phase not in phases:
            raise ValueError("Unknown Google edit result phase.")
        if destination_id is not None and (
            not isinstance(destination_id, str) or not 1 <= len(destination_id) <= 200
        ):
            raise ValueError("Destination ID is invalid.")
        unresolved = {"destination_created", "shared", "applying", "partial", "uncertain"}
        if phase in unresolved and destination_id is None:
            current = await self.edit_result(chat, action_id)
            if not (phase == "uncertain" and current and current.get("phase") == "reserved"):
                destination_id = current.get("destination_id") if current else None
            if (
                phase in {"destination_created", "shared", "applying", "partial"}
                and not destination_id
            ):
                raise ValueError("Unresolved destination phase requires a destination ID.")
        if failed_stage is not None and (
            not isinstance(failed_stage, str) or len(failed_stage) > 64
        ):
            raise ValueError("Failed stage is invalid.")
        if error_category is not None and (
            not isinstance(error_category, str) or len(error_category) > 64
        ):
            raise ValueError("Error category is invalid.")
        if self.db is not None:
            async with self.db.transaction() as tx:
                row = await tx.fetchrow(
                    "SELECT phase,destination_id,kind FROM google_edit_results "
                    "WHERE chat_key=$1 AND action_id=$2 FOR UPDATE",
                    chat,
                    action_id,
                )
                if row is None:
                    raise ValueError("Google edit reservation was not found.")
                old_phase, old_destination = row["phase"], row["destination_id"]
                allowed = {
                    "reserved": {
                        "destination_created",
                        "applying",
                        "conflict",
                        "partial",
                        "uncertain",
                    },
                    "destination_created": {
                        "shared",
                        "applying",
                        "conflict",
                        "partial",
                        "uncertain",
                    },
                    "shared": {"applying", "partial", "uncertain"},
                    "applying": {"verified", "conflict", "partial", "uncertain"},
                    "verified": set(),
                    "conflict": set(),
                    "partial": set(),
                    "uncertain": set(),
                }
                if row["kind"] in {"edit_doc", "create_doc"}:
                    allowed["shared"].add("verified")
                    allowed["applying"].add("shared")
                if old_phase != phase and phase not in allowed.get(old_phase, set()):
                    raise ValueError("Google edit result is already terminal.")
                if destination_id and old_destination and old_destination != destination_id:
                    raise ValueError("Google edit destination cannot be changed after creation.")
                await tx.execute(
                    "UPDATE google_edit_results SET phase=$3,"
                    "destination_id=COALESCE(destination_id,$4),"
                    "failed_stage=$5,error_category=$6,updated_at=now() "
                    "WHERE chat_key=$1 AND action_id=$2",
                    chat,
                    action_id,
                    phase,
                    destination_id,
                    failed_stage,
                    error_category,
                )
            return await self.edit_result(chat, action_id)
        key = (chat, action_id)
        current = self._edit_results.get(key)
        if current is None:
            raise ValueError("Google edit reservation was not found.")
        old_phase, old_destination = current["phase"], current["destination_id"]
        allowed = {
            "reserved": {"destination_created", "applying", "conflict", "partial", "uncertain"},
            "destination_created": {"shared", "applying", "conflict", "partial", "uncertain"},
            "shared": {"applying", "partial", "uncertain"},
            "applying": {"verified", "conflict", "partial", "uncertain"},
            "verified": set(),
            "conflict": set(),
            "partial": set(),
            "uncertain": set(),
        }
        if current["kind"] in {"edit_doc", "create_doc"}:
            allowed["shared"].add("verified")
            allowed["applying"].add("shared")
        if old_phase != phase and phase not in allowed.get(old_phase, set()):
            raise ValueError("Google edit result phase transition is not allowed.")
        if destination_id and old_destination and old_destination != destination_id:
            raise ValueError("Google edit destination cannot be changed after creation.")
        current.update(
            phase=phase,
            destination_id=old_destination or destination_id,
            failed_stage=failed_stage,
            error_category=error_category,
            updated_at=datetime.now(UTC),
        )
        return dict(current)

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
                "DELETE FROM google_create_reservations WHERE chat_key=$1 "
                "AND ($2::text IS NULL OR requester_key=$2)",
                chat,
                user,
            )
            await self.db.execute(
                "DELETE FROM google_outputs WHERE chat_key=$1 "
                "AND ($2::text IS NULL OR requester_key=$2)",
                chat,
                user,
            )
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
            self._create_reservations = {
                key: value
                for key, value in self._create_reservations.items()
                if key[0] != chat or user is not None and value["requester_key"] != user
            }
            self._outputs = {
                key: value
                for key, value in self._outputs.items()
                if key[0] != chat or user is not None and value["requester_key"] != user
            }
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
                self._confirm_writes.pop(chat, None)
            self._edit_results = {k: v for k, v in self._edit_results.items() if k in self._actions}

    async def purge_older_than(self, cutoff, *, unresolved_before=None):
        unresolved_before = unresolved_before or datetime.now(UTC) - timedelta(
            days=self.unresolved_retention_days
        )
        if self.db is not None:
            await self.db.execute(
                "DELETE FROM google_create_reservations WHERE created_at<$1", cutoff
            )
            await self.db.execute("DELETE FROM google_outputs WHERE created_at<$1", cutoff)
            await self.db.execute(
                "DELETE FROM google_actions a WHERE a.created_at<$1 AND NOT EXISTS ("
                "SELECT 1 FROM google_edit_results r WHERE r.chat_key=a.chat_key "
                "AND r.action_id=a.action_id AND r.phase IN "
                "('reserved','destination_created','shared','applying','partial','uncertain') "
                "AND r.created_at >= $2)",
                cutoff,
                unresolved_before,
            )
        else:
            self._create_reservations = {
                key: row
                for key, row in self._create_reservations.items()
                if row["created_at"] >= cutoff
            }
            self._outputs = {
                key: value for key, value in self._outputs.items() if value["created_at"] >= cutoff
            }
            keep = {
                k
                for k, result in self._edit_results.items()
                if result["phase"]
                in {"reserved", "destination_created", "shared", "applying", "partial", "uncertain"}
                and result["created_at"] >= unresolved_before
            }
            self._actions = {k: v for k, v in self._actions.items() if v[0] >= cutoff or k in keep}
            self._edit_results = {k: v for k, v in self._edit_results.items() if k in self._actions}
