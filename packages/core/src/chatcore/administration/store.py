"""Separate, revisioned storage for global group access and setup assignments."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Protocol

from chatcore.events import ChatRef, UserRef
from chatcore.limits import (
    DAILY_BUDGET_MAX,
    DAILY_BUDGET_MIN,
    EXTRA_ALLOWANCE_MAX,
    EXTRA_ALLOWANCE_MIN,
    BudgetOverride,
)
from chatcore.store.base import ChatSettings, CoreRepo


class StaleChangeError(RuntimeError):
    """The displayed administration state changed before confirmation."""


@dataclass(frozen=True, slots=True)
class AccessSnapshot:
    revision: int
    overrides: Mapping[str, bool]
    assignments: frozenset[tuple[str, str]]
    moderators: frozenset[tuple[str, str]] = frozenset()
    restrictions: Mapping[tuple[str, str], Restriction] = field(
        default_factory=lambda: MappingProxyType({})
    )
    user_limits: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    budget: BudgetOverride | None = None


@dataclass(frozen=True, slots=True)
class Restriction:
    user: str
    scope: str
    kind: str
    until: datetime | None
    set_by: str
    set_at: datetime


@dataclass(frozen=True, slots=True)
class AdminChange:
    operation: str
    group: ChatRef
    user: UserRef | None = None
    value: str | bool | int | None = None
    day: date | None = None


@dataclass(frozen=True, slots=True)
class AuditEntry:
    revision: int
    actor: str
    operation: str
    group: str
    subject: str | None
    created_at: datetime
    detail: str | None = None


def global_scope(platform: str) -> ChatRef:
    return ChatRef(platform, "*")


def is_global_scope(ref: ChatRef) -> bool:
    return ref.id == "*"


class AdministrationStore(Protocol):
    async def snapshot(self) -> AccessSnapshot: ...

    async def audit(
        self, *, groups: frozenset[str] | None = None, limit: int = 20
    ) -> tuple[AuditEntry, ...]: ...

    async def apply(
        self,
        *,
        actor: UserRef,
        owners: frozenset[str],
        expected_revision: int,
        change: AdminChange,
        protected: frozenset[str] = frozenset(),
    ) -> AccessSnapshot: ...


def authorize_change(
    snapshot: AccessSnapshot, actor: UserRef, owners: frozenset[str], change: AdminChange
) -> None:
    op = change.operation
    owner_operations = {
        "grant",
        "revoke",
        "moderator_grant",
        "moderator_revoke",
        "limit",
        "limit_reset",
        "budget_set",
        "budget_reset",
        "budget_extra",
        "budget_extra_reset",
    }
    if op in {"block", "pause", "unblock"} and is_global_scope(change.group):
        owner_operations.add(op)
    if actor.key in owners:
        return
    if op in owner_operations:
        raise PermissionError("Owner access required")
    if op in {"block", "pause", "unblock"}:
        if (actor.key, change.group.key) not in snapshot.moderators:
            raise PermissionError("Moderation access required for this group")
        return
    if (actor.key, change.group.key) not in snapshot.assignments:
        raise PermissionError("Setup access required for this group")


def check_target(
    snapshot: AccessSnapshot,
    actor: UserRef,
    owners: frozenset[str],
    protected: frozenset[str],
    change: AdminChange,
) -> None:
    target = change.user
    if target is None:
        return
    if target.key in owners:
        raise ValueError("Owners cannot be administration targets")
    if target.key == actor.key:
        raise ValueError("Administrators cannot target themselves")
    if actor.key not in owners and (
        target.key in protected or (target.key, change.group.key) in snapshot.moderators
    ):
        raise ValueError("This user cannot be targeted by a moderator")


def validate_change(change: AdminChange) -> None:
    op = change.operation
    if op != "budget_extra" and change.day is not None:
        raise ValueError("Unexpected operation day")
    if op == "budget_extra" and type(change.day) is not date:
        raise ValueError("Budget extra requires a date")
    operations = {
        "enable",
        "disable",
        "grant",
        "revoke",
        "chatmode",
        "persona",
        "persona_reset",
        "moderator_grant",
        "moderator_revoke",
        "block",
        "pause",
        "unblock",
        "limit",
        "limit_reset",
        "budget_set",
        "budget_reset",
        "budget_extra",
        "budget_extra_reset",
    }
    if op not in operations:
        raise ValueError("Unknown administration operation")
    group = change.group
    budget_operations = {"budget_set", "budget_reset", "budget_extra", "budget_extra_reset"}
    global_allowed = {"block", "pause", "unblock", "limit", "limit_reset", *budget_operations}
    if is_global_scope(group):
        if not group.platform or op not in global_allowed:
            raise ValueError("Global scope is not valid for this operation")
    else:
        _validate_group(group)
    target_operations = {
        "grant",
        "revoke",
        "moderator_grant",
        "moderator_revoke",
        "block",
        "pause",
        "unblock",
        "limit",
        "limit_reset",
    }
    if op in target_operations:
        if change.user is None:
            raise ValueError("Operation requires exactly one user target")
        _validate_user(change.user)
        if change.user.platform != group.platform:
            raise ValueError("Targets must use the same platform")
        if op in {"pause", "limit"}:
            value = change.value
            if type(value) is not int:
                raise ValueError("Operation value must be an integer")
            if op == "pause" and not 60 <= value <= 30 * 86400:
                raise ValueError("Pause duration must be between 60 seconds and 30 days")
            if op == "limit" and (not 1 <= value <= 10000 or not is_global_scope(group)):
                raise ValueError("Limit must be between 1 and 10000 per hour globally")
        elif change.value is not None:
            raise ValueError("Unexpected operation value")
        if op in {"limit", "limit_reset"} and not is_global_scope(group):
            raise ValueError("Limits require global scope")
        return
    if op in budget_operations:
        if not is_global_scope(group):
            raise ValueError("Budget operations require global scope")
        if change.user is not None:
            raise ValueError("Budget operations do not have a user target")
        if op in {"budget_set", "budget_extra"}:
            bounds = (
                (DAILY_BUDGET_MIN, DAILY_BUDGET_MAX)
                if op == "budget_set"
                else (EXTRA_ALLOWANCE_MIN, EXTRA_ALLOWANCE_MAX)
            )
            if type(change.value) is not int or not bounds[0] <= change.value <= bounds[1]:
                raise ValueError("Budget value is outside the allowed range")
        elif change.value is not None:
            raise ValueError("Unexpected operation value")
        return
    if change.user is not None:
        raise ValueError("Unexpected user target")
    if op in {"enable", "disable", "persona_reset"} and change.value is not None:
        raise ValueError("Unexpected operation value")
    if op == "chatmode" and change.value not in {"on", "off"}:
        raise ValueError("Chatmode must be on or off")
    if op == "persona" and (not isinstance(change.value, str) or len(change.value) > 500):
        raise ValueError("Persona must be text of at most 500 characters")


def _validate_group(group: ChatRef) -> None:
    if not group.platform or not group.id or is_global_scope(group) or ":" in group.id:
        raise ValueError("Invalid group target")
    if group.platform == "tg" and not re.fullmatch(r"-[1-9][0-9]*", group.id):
        raise ValueError("Telegram group IDs must be negative decimal IDs")


def _validate_user(user: UserRef) -> None:
    if not user.platform or not user.id or user.id == "*" or ":" in user.id:
        raise ValueError("Invalid user target")
    if user.platform == "tg" and not re.fullmatch(r"[1-9][0-9]*", user.id):
        raise ValueError("Telegram user IDs must be positive decimal IDs")


class MemoryAdministrationStore:
    def __init__(self, repo: CoreRepo | None = None) -> None:
        self.repo = repo
        self._lock = asyncio.Lock()
        self._revision = 0
        self._overrides: dict[str, bool] = {}
        self._assignments: set[tuple[str, str]] = set()
        self._moderators: set[tuple[str, str]] = set()
        self._restrictions: dict[tuple[str, str], Restriction] = {}
        self._user_limits: dict[str, int] = {}
        self._budget: BudgetOverride | None = None
        self._audit: list[AuditEntry] = []

    async def snapshot(self) -> AccessSnapshot:
        async with self._lock:
            return AccessSnapshot(
                self._revision,
                MappingProxyType(dict(self._overrides)),
                frozenset(self._assignments),
                frozenset(self._moderators),
                MappingProxyType(dict(self._restrictions)),
                MappingProxyType(dict(self._user_limits)),
                self._budget,
            )

    async def audit(
        self, *, groups: frozenset[str] | None = None, limit: int = 20
    ) -> tuple[AuditEntry, ...]:
        async with self._lock:
            if limit <= 0:
                return ()
            entries = [e for e in self._audit if groups is None or e.group in groups]
            return tuple(reversed(entries[-max(0, limit) :]))

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
        async with self._lock:
            if expected_revision != self._revision:
                raise StaleChangeError("Administration settings changed; please retry")
            current = self._snapshot_unlocked()
            authorize_change(current, actor, owners, change)
            check_target(current, actor, owners, protected, change)
            if change.operation == "grant" and change.user.key in owners:
                raise ValueError("Owners cannot be changed with setup assignments")
            if change.operation == "revoke" and change.user.key in owners:
                raise ValueError("Owners cannot be changed with setup assignments")
            subject: str | None = change.user.key if change.user else None
            detail = None
            if change.operation == "enable":
                self._overrides[change.group.key] = True
            elif change.operation == "disable":
                self._overrides[change.group.key] = False
            elif change.operation == "grant":
                self._assignments.add((change.user.key, change.group.key))
            elif change.operation == "revoke":
                self._assignments.discard((change.user.key, change.group.key))
            elif change.operation == "moderator_grant":
                self._moderators.add((change.user.key, change.group.key))
            elif change.operation == "moderator_revoke":
                self._moderators.discard((change.user.key, change.group.key))
            elif change.operation in {"block", "pause"}:
                now = datetime.now(UTC)
                until = (
                    now + timedelta(seconds=change.value) if change.operation == "pause" else None
                )
                self._restrictions[(change.user.key, change.group.key)] = Restriction(
                    change.user.key,
                    change.group.key,
                    "pause" if until else "block",
                    until,
                    actor.key,
                    now,
                )
                if until:
                    detail = f"until={until.isoformat().replace('+00:00', 'Z')}"
            elif change.operation == "unblock":
                self._restrictions.pop((change.user.key, change.group.key), None)
            elif change.operation == "limit":
                self._user_limits[change.user.key] = change.value
                detail = f"limit={change.value}"
            elif change.operation == "limit_reset":
                self._user_limits.pop(change.user.key, None)
            elif change.operation in {
                "budget_set",
                "budget_reset",
                "budget_extra",
                "budget_extra_reset",
            }:
                old = self._budget or BudgetOverride(None, None, None, None, None)
                daily_tokens = (
                    change.value
                    if change.operation == "budget_set"
                    else (None if change.operation == "budget_reset" else old.daily_tokens)
                )
                extra_tokens = (
                    change.value
                    if change.operation == "budget_extra"
                    else (None if change.operation == "budget_extra_reset" else old.extra_tokens)
                )
                extra_day = (
                    change.day
                    if change.operation == "budget_extra"
                    else (None if change.operation == "budget_extra_reset" else old.extra_day)
                )
                now = datetime.now(UTC)
                self._budget = BudgetOverride(daily_tokens, extra_tokens, extra_day, actor.key, now)
                if change.operation == "budget_set":
                    detail = f"budget={change.value}"
                elif change.operation == "budget_reset":
                    detail = "budget=default"
                elif change.operation == "budget_extra":
                    detail = f"extra={change.value} day={change.day.isoformat()}"
                else:
                    detail = "extra=none"
            elif change.operation in {"chatmode", "persona", "persona_reset"}:
                if self.repo is None:
                    raise RuntimeError("Chat settings repository is required")
                settings = await self.repo.get_chat_settings(change.group)
                if change.operation == "chatmode":
                    settings = ChatSettings(change.group, settings.persona, change.value == "on")
                else:
                    settings = ChatSettings(
                        change.group,
                        None if change.operation == "persona_reset" else str(change.value),
                        settings.chatmode,
                    )
                await self.repo.save_chat_settings(settings, updated_by=actor)
            self._revision += 1
            self._audit.append(
                AuditEntry(
                    self._revision,
                    actor.key,
                    change.operation,
                    change.group.key,
                    subject,
                    datetime.now(UTC),
                    detail,
                )
            )
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> AccessSnapshot:
        return AccessSnapshot(
            self._revision,
            MappingProxyType(dict(self._overrides)),
            frozenset(self._assignments),
            frozenset(self._moderators),
            MappingProxyType(dict(self._restrictions)),
            MappingProxyType(dict(self._user_limits)),
            self._budget,
        )
