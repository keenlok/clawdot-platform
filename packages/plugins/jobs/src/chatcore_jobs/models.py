"""Immutable scope and persisted job value types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Scope:
    chat_key: str
    is_dm: bool
    thread_id: str | None = None

    def __post_init__(self) -> None:
        if not self.chat_key or type(self.is_dm) is not bool:
            raise ValueError("invalid scope")
        if self.thread_id is not None and not self.thread_id:
            raise ValueError("invalid thread")


@dataclass(frozen=True, slots=True)
class Actor:
    user_key: str

    def __post_init__(self) -> None:
        if not self.user_key:
            raise ValueError("invalid actor")


class JobKind(StrEnum):
    REMINDER = "reminder"
    WATCH = "watch"


class JobStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Job:
    id: UUID
    scope: Scope
    owner: Actor
    kind: JobKind
    status: JobStatus
    payload: dict[str, Any]
    created_at: datetime
    next_run_at: datetime
    occurrence: int = 0
    revision: int = 0
    last_delivery_state: str | None = None
    snapshot: str | None = None
    snapshot_hash: str | None = None
    failure_count: int = 0
    error_episode: bool = False
    terminal_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.next_run_at.tzinfo is None:
            raise ValueError("job timestamps must be aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        object.__setattr__(self, "next_run_at", self.next_run_at.astimezone(UTC))
        if self.terminal_at is not None:
            if self.terminal_at.tzinfo is None:
                raise ValueError("terminal time must be aware")
            object.__setattr__(self, "terminal_at", self.terminal_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class Claim:
    job: Job
    token: UUID
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class Delivery:
    job_id: UUID
    occurrence: int
    notification_kind: str
    status: str
    content: str
