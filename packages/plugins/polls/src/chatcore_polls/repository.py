"""Scoped Postgres persistence, serialized reservations and fenced close leases."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from chatcore.events import User
from chatcore.store.pg import PgDatabase

from chatcore_polls.models import Poll, PollDraft, Scope
from chatcore_polls.settings import PollsSettings


class PollConflict(ValueError):
    """The scope, current settings or poll quota no longer permits the write."""


def _poll(row: Any) -> Poll:
    values = dict(row)
    scope = Scope(values.pop("chat_key"), values.pop("is_dm"), values.pop("thread_id"))
    for key in ("options", "final_counts"):
        value = values[key]
        if isinstance(value, str):
            value = json.loads(value)
        values[key] = tuple(value) if value is not None else None
    return Poll(scope=scope, **values)


class PollsRepo:
    def __init__(self, db: PgDatabase, settings: PollsSettings) -> None:
        if not isinstance(db, PgDatabase):
            raise TypeError("PollsRepo requires PgDatabase")
        self.db, self.config = db, settings

    async def settings(self, scope: Scope) -> dict[str, Any]:
        row = await self.db.fetchrow(
            "SELECT * FROM poll_settings WHERE chat_key=$1 AND is_dm=$2",
            scope.chat_key,
            scope.is_dm,
        )
        return dict(row) if row else {"polls_enabled": False, "revision": 0}

    async def configure(
        self,
        scope: Scope,
        enabled: bool,
        expected_revision: int,
    ) -> dict[str, Any]:
        if scope.is_dm or type(enabled) is not bool or type(expected_revision) is not int:
            raise PollConflict("Invalid group poll settings.")
        async with self.db.transaction() as tx:
            await tx.execute(
                "SELECT pg_advisory_xact_lock(hashtext('polls:' || $1))", scope.chat_key
            )
            await tx.execute(
                "INSERT INTO poll_settings (chat_key,is_dm) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                scope.chat_key,
                scope.is_dm,
            )
            row = await tx.fetchrow(
                "UPDATE poll_settings SET polls_enabled=$3,revision=revision+1,updated_at=now() "
                "WHERE chat_key=$1 AND is_dm=$2 AND revision=$4 RETURNING *",
                scope.chat_key,
                scope.is_dm,
                enabled,
                expected_revision,
            )
            if not row:
                raise PollConflict("Poll settings changed; request the change again.")
            return dict(row)

    async def reserve(
        self,
        scope: Scope,
        creator: User,
        draft: PollDraft,
        source_action_id: str | None,
        now: datetime,
        *,
        expected_revision: int | None = None,
    ) -> tuple[Poll, bool]:
        """Return (row, newly_reserved); only a new reservation may send to the platform."""
        if scope.is_dm or creator.ref.platform != scope.chat.platform:
            raise PollConflict("Polls work in group chats.")
        async with self.db.transaction() as tx:
            await tx.execute(
                "SELECT pg_advisory_xact_lock(hashtext('polls:' || $1))", scope.chat_key
            )
            if source_action_id is not None:
                existing = await tx.fetchrow(
                    "SELECT * FROM polls WHERE chat_key=$1 AND source_action_id=$2",
                    scope.chat_key,
                    source_action_id,
                )
                if existing:
                    poll = _poll(existing)
                    if poll.scope != scope or poll.creator_key != creator.ref.key:
                        raise PollConflict("Poll request scope changed.")
                    return poll, False
            config = await tx.fetchrow(
                "SELECT * FROM poll_settings WHERE chat_key=$1 AND is_dm=$2",
                scope.chat_key,
                scope.is_dm,
            )
            if (
                not config
                or not config["polls_enabled"]
                or (expected_revision is not None and config["revision"] != expected_revision)
            ):
                raise PollConflict("Poll settings changed; nothing was posted.")
            counts = await tx.fetchrow(
                "SELECT count(*) AS chat_count, "
                "count(*) FILTER (WHERE creator_key=$3) AS user_count "
                "FROM polls WHERE chat_key=$1 AND is_dm=$2 AND status IN ('sending','open')",
                scope.chat_key,
                scope.is_dm,
                creator.ref.key,
            )
            if counts["chat_count"] >= self.config.max_open_per_chat:
                raise PollConflict("This group has reached its open poll limit.")
            if counts["user_count"] >= self.config.max_open_per_user:
                raise PollConflict("You have reached your open poll limit in this group.")
            row = await tx.fetchrow(
                """INSERT INTO polls (id,chat_key,is_dm,thread_id,number,creator_key,creator_name,
                    question,options,multiple,anonymous,status,close_at,source_action_id,created_at)
                   VALUES ($1,$2,$3,$4,
                    (SELECT COALESCE(max(number),0)+1 FROM polls WHERE chat_key=$2),
                    $5,$6,$7,$8::jsonb,$9,$10,'sending',$11,$12,$13) RETURNING *""",
                uuid4(),
                scope.chat_key,
                scope.is_dm,
                scope.thread_id,
                creator.ref.key,
                creator.display_name or (f"@{creator.username}" if creator.username else ""),
                draft.question,
                json.dumps(draft.options),
                draft.multiple,
                draft.anonymous,
                draft.close_at,
                source_action_id,
                now,
            )
            return _poll(row), True

    async def mark_open(
        self,
        scope: Scope,
        poll_id: UUID,
        message_id: str,
        platform_poll_id: str,
    ) -> bool:
        return bool(
            await self.db.fetchval(
                "UPDATE polls SET status='open',platform_message_id=$5,platform_poll_id=$6 "
                "WHERE chat_key=$1 AND is_dm=$2 AND thread_id IS NOT DISTINCT FROM $3 "
                "AND id=$4 AND status='sending' RETURNING id",
                scope.chat_key,
                scope.is_dm,
                scope.thread_id,
                poll_id,
                message_id,
                platform_poll_id,
            )
        )

    async def mark_failed(self, scope: Scope, poll_id: UUID) -> None:
        await self.db.execute(
            "UPDATE polls SET status='failed' WHERE chat_key=$1 AND is_dm=$2 "
            "AND thread_id IS NOT DISTINCT FROM $3 AND id=$4 AND status='sending'",
            scope.chat_key,
            scope.is_dm,
            scope.thread_id,
            poll_id,
        )

    async def fail_stale_sending(self, now: datetime) -> int:
        """Maintenance sweep: retire abandoned reservations across all chat scopes."""
        return int(
            await self.db.fetchval(
                "WITH failed AS (UPDATE polls SET status='failed',closed_at=$1 "
                "WHERE status='sending' AND created_at < $2 RETURNING id) "
                "SELECT count(*) FROM failed",
                now,
                now - timedelta(minutes=10),
            )
        )

    async def list_open(self, scope: Scope) -> list[Poll]:
        rows = await self.db.fetch(
            "SELECT * FROM polls WHERE chat_key=$1 AND is_dm=$2 "
            "AND thread_id IS NOT DISTINCT FROM $3 AND status='open' ORDER BY number",
            scope.chat_key,
            scope.is_dm,
            scope.thread_id,
        )
        return [_poll(row) for row in rows]

    async def get_by_number(self, scope: Scope, number: int) -> Poll | None:
        row = await self.db.fetchrow(
            "SELECT * FROM polls WHERE chat_key=$1 AND is_dm=$2 "
            "AND thread_id IS NOT DISTINCT FROM $3 AND number=$4",
            scope.chat_key,
            scope.is_dm,
            scope.thread_id,
            number,
        )
        return _poll(row) if row else None

    async def get_by_message(self, scope: Scope, message_id: str) -> Poll | None:
        row = await self.db.fetchrow(
            "SELECT * FROM polls WHERE chat_key=$1 AND is_dm=$2 "
            "AND thread_id IS NOT DISTINCT FROM $3 AND platform_message_id=$4",
            scope.chat_key,
            scope.is_dm,
            scope.thread_id,
            message_id,
        )
        return _poll(row) if row else None

    async def find_open_by_platform_poll(self, platform: str, poll_id: str) -> Poll | None:
        """Trusted adapter event lookup: the event has a platform namespace but no chat."""
        row = await self.db.fetchrow(
            "SELECT * FROM polls WHERE split_part(chat_key,':',1)=$1 "
            "AND platform_poll_id=$2 AND status='open'",
            platform,
            poll_id,
        )
        return _poll(row) if row else None

    async def replace_votes(
        self,
        poll: Poll,
        voter: User,
        option_ids: tuple[int, ...],
        now: datetime,
    ) -> bool:
        if (
            voter.ref.platform != poll.scope.chat.platform
            or any(type(i) is not int or not 0 <= i < len(poll.options) for i in option_ids)
            or len(set(option_ids)) != len(option_ids)
            or (not poll.multiple and len(option_ids) > 1)
        ):
            raise ValueError("Invalid poll answer.")
        scope = poll.scope
        async with self.db.transaction() as tx:
            row = await tx.fetchrow(
                "SELECT id FROM polls WHERE chat_key=$1 AND is_dm=$2 "
                "AND thread_id IS NOT DISTINCT FROM $3 AND id=$4 "
                "AND status='open' AND NOT anonymous FOR UPDATE",
                scope.chat_key,
                scope.is_dm,
                scope.thread_id,
                poll.id,
            )
            if not row:
                return False
            await tx.execute(
                "DELETE FROM poll_votes WHERE chat_key=$1 AND poll_id=$2 AND voter_key=$3",
                scope.chat_key,
                poll.id,
                voter.ref.key,
            )
            for index in option_ids:
                await tx.execute(
                    "INSERT INTO poll_votes (poll_id,chat_key,voter_key,voter_name,option_index,"
                    "voted_at) VALUES ($1,$2,$3,$4,$5,$6)",
                    poll.id,
                    scope.chat_key,
                    voter.ref.key,
                    voter.display_name or (f"@{voter.username}" if voter.username else ""),
                    index,
                    now,
                )
            return True

    async def claim_close(
        self,
        poll_id: UUID | None,
        now: datetime,
        limit: int,
        *,
        scope: Scope | None = None,
        allowed_chat_keys: list[str] | None = None,
    ) -> list[Poll]:
        """Global due scan, or a manual claim that must include the exact command scope."""
        if poll_id is not None and scope is None:
            raise ValueError("Manual close requires a scope.")
        rows = await self.db.fetch(
            """WITH due AS (
                 SELECT id FROM polls WHERE status='open' AND platform_message_id IS NOT NULL
                 AND ($9::text[] IS NULL OR chat_key=ANY($9))
                 AND (lease_until IS NULL OR lease_until <= $2)
                 AND (($1::uuid IS NULL AND close_at <= $2) OR
                      (id=$1 AND chat_key=$6 AND is_dm=$7
                       AND thread_id IS NOT DISTINCT FROM $8))
                 ORDER BY close_at NULLS LAST,id FOR UPDATE SKIP LOCKED LIMIT $3
               ) UPDATE polls p SET lease_token=$4,lease_until=$5,
                 close_at=CASE WHEN $1::uuid IS NULL THEN p.close_at ELSE $2 END
                 FROM due WHERE p.id=due.id RETURNING p.*""",
            poll_id,
            now,
            limit,
            uuid4(),
            now + timedelta(seconds=self.config.lease_seconds),
            scope.chat_key if scope else None,
            scope.is_dm if scope else None,
            scope.thread_id if scope else None,
            allowed_chat_keys,
        )
        return [_poll(row) for row in rows]

    async def finish_close(
        self,
        scope: Scope,
        poll_id: UUID,
        lease_token: UUID,
        counts: tuple[int, ...] | None,
        total: int | None,
        now: datetime,
    ) -> bool:
        return bool(
            await self.db.fetchval(
                "UPDATE polls SET status='closed',final_counts=$6::jsonb,"
                "total_voters=$7,closed_at=$8,"
                "lease_token=NULL,lease_until=NULL WHERE chat_key=$1 AND is_dm=$2 "
                "AND thread_id IS NOT DISTINCT FROM $3 AND id=$4 AND lease_token=$5 "
                "AND status='open' RETURNING id",
                scope.chat_key,
                scope.is_dm,
                scope.thread_id,
                poll_id,
                lease_token,
                json.dumps(counts) if counts is not None else None,
                total,
                now,
            )
        )

    async def record_close_failure(
        self,
        scope: Scope,
        poll_id: UUID,
        lease_token: UUID,
    ) -> int | None:
        return await self.db.fetchval(
            "UPDATE polls SET close_failures=close_failures+1 WHERE chat_key=$1 AND is_dm=$2 "
            "AND thread_id IS NOT DISTINCT FROM $3 AND id=$4 AND lease_token=$5 "
            "AND status='open' RETURNING close_failures",
            scope.chat_key,
            scope.is_dm,
            scope.thread_id,
            poll_id,
            lease_token,
        )

    async def votes(self, scope: Scope, poll_id: UUID) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT v.* FROM poll_votes v JOIN polls p ON p.id=v.poll_id "
            "AND p.chat_key=v.chat_key WHERE p.chat_key=$1 AND p.is_dm=$2 "
            "AND p.thread_id IS NOT DISTINCT FROM $3 AND p.id=$4 ORDER BY v.voted_at,v.voter_key",
            scope.chat_key,
            scope.is_dm,
            scope.thread_id,
            poll_id,
        )
        return [dict(row) for row in rows]

    async def purge_user(self, chat_key: str, user_key: str) -> None:
        async with self.db.transaction() as tx:
            await tx.execute(
                "DELETE FROM poll_votes WHERE chat_key=$1 AND voter_key=$2", chat_key, user_key
            )
            await tx.execute(
                "DELETE FROM polls WHERE chat_key=$1 AND creator_key=$2", chat_key, user_key
            )

    async def purge_chat(self, chat_key: str) -> None:
        async with self.db.transaction() as tx:
            await tx.execute("DELETE FROM polls WHERE chat_key=$1", chat_key)
            await tx.execute("DELETE FROM poll_settings WHERE chat_key=$1", chat_key)

    async def purge_older_than(self, cutoff: datetime) -> None:
        await self.db.execute(
            "DELETE FROM polls WHERE (status='closed' AND closed_at < $1) OR "
            "(status='failed' AND COALESCE(closed_at,created_at) < $1)",
            cutoff,
        )
