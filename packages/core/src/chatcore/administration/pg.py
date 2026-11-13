"""Postgres administration store with transactional revision and audit updates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from chatcore.administration.store import (
    AccessSnapshot,
    AdminChange,
    AdmissionResult,
    AuditEntry,
    GroupAdmission,
    Restriction,
    StaleChangeError,
    authorize_change,
    authorize_inviter_batch,
    check_target,
    group_admission_decision,
    inviter_batch_changes,
    validate_change,
)
from chatcore.events import ChatRef, UserRef
from chatcore.limits import BudgetOverride
from chatcore.store.base import Database


class PgAdministrationStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def snapshot(self) -> AccessSnapshot:
        async with self.db.transaction() as tx:
            revision = await tx.fetchval(
                "SELECT revision FROM administration_revision WHERE singleton=true FOR SHARE"
            )
            if revision is None:
                raise RuntimeError("Administration revision row is missing")
            return await self._snapshot(tx, revision)

    async def _snapshot(self, tx: Database, revision: int) -> AccessSnapshot:
        overrides = await tx.fetch("SELECT chat_key, enabled FROM administration_groups")
        assignments = await tx.fetch("SELECT user_key, chat_key FROM administration_assignments")
        moderators = await tx.fetch("SELECT user_key, chat_key FROM administration_moderators")
        restrictions = await tx.fetch(
            "SELECT user_key, scope_key, kind, until, set_by, set_at "
            "FROM administration_restrictions"
        )
        user_limits = await tx.fetch("SELECT user_key, per_hour FROM administration_user_limits")
        budget_row = await tx.fetchrow(
            "SELECT daily_tokens, extra_tokens, extra_day, set_by, set_at "
            "FROM administration_budget WHERE singleton=true"
        )
        trusted = await tx.fetch("SELECT user_key FROM administration_trusted_inviters")
        blocks = await tx.fetch("SELECT chat_key FROM administration_group_blocks")
        admissions = await tx.fetch(
            "SELECT chat_key, inviter_key, admitted_at FROM administration_group_admissions"
        )
        return AccessSnapshot(
            revision,
            MappingProxyType({row["chat_key"]: row["enabled"] for row in overrides}),
            frozenset((row["user_key"], row["chat_key"]) for row in assignments),
            frozenset((row["user_key"], row["chat_key"]) for row in moderators),
            MappingProxyType(
                {
                    (row["user_key"], row["scope_key"]): Restriction(
                        row["user_key"],
                        row["scope_key"],
                        row["kind"],
                        row["until"],
                        row["set_by"],
                        row["set_at"],
                    )
                    for row in restrictions
                }
            ),
            MappingProxyType({row["user_key"]: row["per_hour"] for row in user_limits}),
            BudgetOverride(
                budget_row["daily_tokens"],
                budget_row["extra_tokens"],
                budget_row["extra_day"],
                budget_row["set_by"],
                budget_row["set_at"],
            )
            if budget_row
            else None,
            frozenset(row["user_key"] for row in trusted),
            frozenset(row["chat_key"] for row in blocks),
            MappingProxyType(
                {
                    row["chat_key"]: GroupAdmission(row["inviter_key"], row["admitted_at"])
                    for row in admissions
                }
            ),
        )

    async def audit(
        self, *, groups: frozenset[str] | None = None, limit: int = 20
    ) -> tuple[AuditEntry, ...]:
        async with self.db.transaction() as tx:
            revision = await tx.fetchval(
                "SELECT revision FROM administration_revision WHERE singleton=true FOR SHARE"
            )
            if revision is None:
                raise RuntimeError("Administration revision row is missing")
            rows = await tx.fetch(
                """SELECT revision, actor_key, operation, chat_key, subject_key, created_at, detail
                   FROM administration_audit
                   WHERE ($1::text[] IS NULL OR chat_key = ANY($1))
                   ORDER BY id DESC LIMIT $2""",
                sorted(groups) if groups is not None else None,
                max(0, limit),
            )
            return tuple(
                AuditEntry(
                    row["revision"],
                    row["actor_key"],
                    row["operation"],
                    row["chat_key"],
                    row["subject_key"],
                    row["created_at"],
                    row["detail"],
                )
                for row in rows
            )

    async def admit_group(
        self,
        *,
        group: ChatRef,
        inviter: UserRef | None,
        owners: frozenset[str],
        bootstrap_chats: frozenset[str],
        event_key: str | None = None,
    ) -> AdmissionResult:
        async with self.db.transaction() as tx:
            revision = await tx.fetchval(
                "SELECT revision FROM administration_revision WHERE singleton=true FOR UPDATE"
            )
            if revision is None:
                raise RuntimeError("Administration revision row is missing")
            snapshot = await self._snapshot(tx, revision)
            result = group_admission_decision(snapshot, group, inviter, owners, bootstrap_chats)
            if result == "denied":
                return result
            if event_key is not None:
                claimed = await tx.fetchval(
                    "INSERT INTO administration_join_events(chat_key,event_key) VALUES($1,$2) "
                    "ON CONFLICT DO NOTHING RETURNING event_key",
                    group.key,
                    event_key,
                )
                if claimed is None:
                    return "duplicate"
            if result == "admitted":
                await tx.execute(
                    "INSERT INTO administration_groups(chat_key,enabled,updated_by) "
                    "VALUES($1,true,$2)",
                    group.key,
                    inviter.key,
                )
                await tx.execute(
                    "INSERT INTO administration_group_admissions(chat_key,inviter_key) "
                    "VALUES($1,$2) "
                    "ON CONFLICT(chat_key) DO UPDATE SET "
                    "inviter_key=EXCLUDED.inviter_key, admitted_at=now()",
                    group.key,
                    inviter.key,
                )
                await tx.execute(
                    "UPDATE administration_revision SET revision=$1 WHERE singleton=true",
                    revision + 1,
                )
                await tx.execute(
                    "INSERT INTO administration_audit(revision,actor_key,operation,chat_key) "
                    "VALUES($1,$2,'admit_group',$3)",
                    revision + 1,
                    inviter.key,
                    group.key,
                )
            return result

    async def apply_inviter_batch(
        self,
        *,
        actor: UserRef,
        owners: frozenset[str],
        expected_revision: int,
        operation: str,
        users: tuple[UserRef, ...],
        protected: frozenset[str] = frozenset(),
    ) -> AccessSnapshot:
        changes = inviter_batch_changes(actor, operation, users)
        async with self.db.transaction() as tx:
            revision = await tx.fetchval(
                "SELECT revision FROM administration_revision WHERE singleton=true FOR UPDATE"
            )
            if revision is None:
                raise RuntimeError("Administration revision row is missing")
            if revision != expected_revision:
                raise StaleChangeError("Administration settings changed; please retry")
            changes = authorize_inviter_batch(
                await self._snapshot(tx, revision), actor, owners, changes, protected
            )
            if changes:
                revision += 1
                for change in changes:
                    if operation == "trust":
                        await tx.execute(
                            "INSERT INTO administration_trusted_inviters(user_key,granted_by) "
                            "VALUES($1,$2) ON CONFLICT(user_key) DO NOTHING",
                            change.user.key,
                            actor.key,
                        )
                    else:
                        await tx.execute(
                            "DELETE FROM administration_trusted_inviters WHERE user_key=$1",
                            change.user.key,
                        )
                    await tx.execute(
                        "INSERT INTO administration_audit"
                        "(revision,actor_key,operation,chat_key,subject_key) "
                        "VALUES($1,$2,$3,$4,$5)",
                        revision,
                        actor.key,
                        operation,
                        change.group.key,
                        change.user.key,
                    )
                await tx.execute(
                    "UPDATE administration_revision SET revision=$1 WHERE singleton=true", revision
                )
        return await self.snapshot()

    async def apply(
        self,
        *,
        actor: UserRef,
        owners: frozenset[str],
        expected_revision: int,
        change: AdminChange,
        protected: frozenset[str] = frozenset(),
    ) -> AccessSnapshot:
        validate_change(change)
        if actor.platform != change.group.platform:
            raise ValueError("Actor and group must use the same platform")
        async with self.db.transaction() as tx:
            revision = await tx.fetchval(
                "SELECT revision FROM administration_revision WHERE singleton=true FOR UPDATE"
            )
            if revision is None:
                raise RuntimeError("Administration revision row is missing")
            if revision != expected_revision:
                raise StaleChangeError("Administration settings changed; please retry")
            snapshot = await self._snapshot(tx, revision)
            authorize_change(snapshot, actor, owners, change)
            check_target(snapshot, actor, owners, protected, change)
            if change.operation in {"grant", "revoke"} and change.user.key in owners:
                raise ValueError("Owners cannot be changed with setup assignments")

            subject = change.user.key if change.user else None
            op = change.operation
            detail = None
            if op in {"trust", "untrust"}:
                if op == "trust":
                    await tx.execute(
                        "INSERT INTO administration_trusted_inviters(user_key, granted_by) "
                        "VALUES($1,$2) ON CONFLICT(user_key) DO NOTHING",
                        change.user.key,
                        actor.key,
                    )
                else:
                    await tx.execute(
                        "DELETE FROM administration_trusted_inviters WHERE user_key=$1",
                        change.user.key,
                    )
            elif op == "unblockgroup":
                await tx.execute(
                    "DELETE FROM administration_group_blocks WHERE chat_key=$1", change.group.key
                )
            elif op in {"enable", "disable", "blockgroup"}:
                if op == "blockgroup":
                    await tx.execute(
                        "INSERT INTO administration_group_blocks(chat_key,set_by) VALUES($1,$2) "
                        "ON CONFLICT(chat_key) DO NOTHING",
                        change.group.key,
                        actor.key,
                    )
                await tx.execute(
                    """INSERT INTO administration_groups(chat_key, enabled, updated_by)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (chat_key) DO UPDATE
                       SET enabled=EXCLUDED.enabled, updated_by=EXCLUDED.updated_by,
                           updated_at=now()""",
                    change.group.key,
                    op == "enable",
                    actor.key,
                )
            elif op == "grant":
                await tx.execute(
                    """INSERT INTO administration_assignments(user_key, chat_key, granted_by)
                       VALUES ($1, $2, $3) ON CONFLICT (user_key, chat_key) DO NOTHING""",
                    change.user.key,
                    change.group.key,
                    actor.key,
                )
            elif op == "revoke":
                await tx.execute(
                    "DELETE FROM administration_assignments WHERE user_key=$1 AND chat_key=$2",
                    change.user.key,
                    change.group.key,
                )
            elif op in {"moderator_grant", "moderator_revoke"}:
                if op == "moderator_grant":
                    await tx.execute(
                        """INSERT INTO administration_moderators(user_key, chat_key, granted_by)
                           VALUES ($1, $2, $3) ON CONFLICT (user_key, chat_key) DO NOTHING""",
                        change.user.key,
                        change.group.key,
                        actor.key,
                    )
                else:
                    await tx.execute(
                        "DELETE FROM administration_moderators WHERE user_key=$1 AND chat_key=$2",
                        change.user.key,
                        change.group.key,
                    )
            elif op in {"block", "pause"}:
                now = datetime.now(UTC)
                until = now + timedelta(seconds=change.value) if op == "pause" else None
                await tx.execute(
                    """INSERT INTO administration_restrictions
                       (user_key, scope_key, kind, until, set_by, set_at)
                       VALUES ($1, $2, $3, $4, $5, $6)
                       ON CONFLICT (user_key, scope_key) DO UPDATE
                       SET kind=EXCLUDED.kind, until=EXCLUDED.until,
                           set_by=EXCLUDED.set_by, set_at=EXCLUDED.set_at""",
                    change.user.key,
                    change.group.key,
                    "pause" if until else "block",
                    until,
                    actor.key,
                    now,
                )
                if until:
                    detail = f"until={until.isoformat().replace('+00:00', 'Z')}"
            elif op == "unblock":
                await tx.execute(
                    "DELETE FROM administration_restrictions WHERE user_key=$1 AND scope_key=$2",
                    change.user.key,
                    change.group.key,
                )
            elif op in {"limit", "limit_reset"}:
                if op == "limit":
                    await tx.execute(
                        """INSERT INTO administration_user_limits(user_key, per_hour, set_by)
                           VALUES ($1, $2, $3)
                           ON CONFLICT (user_key) DO UPDATE
                           SET per_hour=EXCLUDED.per_hour, set_by=EXCLUDED.set_by,
                               set_at=now()""",
                        change.user.key,
                        change.value,
                        actor.key,
                    )
                    detail = f"limit={change.value}"
                else:
                    await tx.execute(
                        "DELETE FROM administration_user_limits WHERE user_key=$1",
                        change.user.key,
                    )
            elif op in {"budget_set", "budget_reset", "budget_extra", "budget_extra_reset"}:
                daily = change.value if op == "budget_set" else None
                extra = change.value if op == "budget_extra" else None
                extra_day = change.day if op == "budget_extra" else None
                if op in {"budget_set", "budget_reset"}:
                    current = await tx.fetchrow(
                        "SELECT extra_tokens, extra_day "
                        "FROM administration_budget WHERE singleton=true"
                    )
                    extra = current["extra_tokens"] if current else None
                    extra_day = current["extra_day"] if current else None
                else:
                    current = await tx.fetchrow(
                        "SELECT daily_tokens FROM administration_budget WHERE singleton=true"
                    )
                    daily = current["daily_tokens"] if current else None
                await tx.execute(
                    """INSERT INTO administration_budget
                       (singleton, daily_tokens, extra_tokens, extra_day, set_by, set_at)
                       VALUES (true, $1, $2, $3, $4, now())
                       ON CONFLICT (singleton) DO UPDATE SET
                       daily_tokens=EXCLUDED.daily_tokens, extra_tokens=EXCLUDED.extra_tokens,
                       extra_day=EXCLUDED.extra_day, set_by=EXCLUDED.set_by, set_at=now()""",
                    daily,
                    extra,
                    extra_day,
                    actor.key,
                )
                if op == "budget_set":
                    detail = f"budget={change.value}"
                elif op == "budget_reset":
                    detail = "budget=default"
                elif op == "budget_extra":
                    detail = f"extra={change.value} day={change.day.isoformat()}"
                else:
                    detail = "extra=none"
            elif op in {"chatmode", "persona", "persona_reset"}:
                if op == "chatmode":
                    await tx.execute(
                        """INSERT INTO chat_settings(chat_key, chatmode, updated_by)
                           VALUES ($1, $2, $3)
                           ON CONFLICT (chat_key) DO UPDATE
                           SET chatmode=EXCLUDED.chatmode, updated_by=EXCLUDED.updated_by,
                               updated_at=now()""",
                        change.group.key,
                        change.value == "on",
                        actor.key,
                    )
                else:
                    await tx.execute(
                        """INSERT INTO chat_settings(chat_key, persona, updated_by)
                           VALUES ($1, $2, $3)
                           ON CONFLICT (chat_key) DO UPDATE
                           SET persona=EXCLUDED.persona, updated_by=EXCLUDED.updated_by,
                               updated_at=now()""",
                        change.group.key,
                        None if op == "persona_reset" else change.value,
                        actor.key,
                    )

            new_revision = revision + 1
            await tx.execute(
                "UPDATE administration_revision SET revision=$1 WHERE singleton=true",
                new_revision,
            )
            await tx.execute(
                """INSERT INTO administration_audit
                   (revision, actor_key, operation, chat_key, subject_key, detail)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                new_revision,
                actor.key,
                op,
                change.group.key,
                subject,
                detail,
            )
        return await self.snapshot()
