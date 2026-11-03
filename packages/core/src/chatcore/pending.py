"""Pending writes: the hand-off between write tools and the human confirmation (§6a, §7a).

A write tool never commits. It returns ``ToolResult(pending_action=...)``; the core shows exactly
``summary`` to the requester with Confirm/Cancel (or a reply code), and only on their confirmation
calls the owning plugin's ``PendingActionHandler.commit``. Actions are single use and expire.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from chatcore.events import ChatRef, UserRef

if TYPE_CHECKING:
    from chatcore.plugin import CommandContext

DEFAULT_TTL = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PendingAction:
    id: str
    chat: ChatRef
    requester: UserRef
    plugin: str
    kind: str
    payload: dict[str, Any]
    summary: str
    """Exactly what will be written, shown to the requester before they confirm."""
    reply_code: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    thread_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        chat: ChatRef,
        requester: UserRef,
        plugin: str,
        kind: str,
        payload: dict[str, Any],
        summary: str,
        thread_id: str | None = None,
        ttl: timedelta = DEFAULT_TTL,
        now: datetime | None = None,
    ) -> PendingAction:
        now = now or _now()
        return cls(
            id=secrets.token_urlsafe(12),
            chat=chat,
            requester=requester,
            plugin=plugin,
            kind=kind,
            payload=payload,
            summary=summary,
            reply_code=f"{secrets.randbelow(10_000):04d}",
            created_at=now,
            expires_at=now + ttl,
            thread_id=thread_id,
        )

    @property
    def handler_key(self) -> str:
        return f"{self.plugin}.{self.kind}"

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or _now()) >= self.expires_at

    def mark_used(self, now: datetime | None = None) -> PendingAction:
        return replace(self, used_at=now or _now())


class PendingActionHandler(Protocol):
    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        """Perform the write. Returns the confirmation text shown in chat."""


@dataclass(slots=True)
class ConfirmOutcome:
    status: str  # "committed" | "cancelled" | "ignored" | "expired" | "failed"
    message: str | None = None
    action: PendingAction | None = field(default=None)
