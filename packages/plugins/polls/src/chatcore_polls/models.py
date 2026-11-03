"""Immutable scope, draft, and stored poll values."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from chatcore.events import ChatRef


@dataclass(frozen=True, slots=True)
class Scope:
    chat_key: str
    is_dm: bool
    thread_id: str | None = None

    @property
    def chat(self) -> ChatRef:
        return ChatRef.parse(self.chat_key)


@dataclass(frozen=True, slots=True)
class PollDraft:
    question: str
    options: tuple[str, ...]
    multiple: bool = False
    anonymous: bool = False
    close_at: datetime | None = None
    close_label: str | None = None


@dataclass(frozen=True, slots=True)
class Poll:
    id: UUID
    scope: Scope
    number: int
    creator_key: str
    creator_name: str
    question: str
    options: tuple[str, ...]
    multiple: bool
    anonymous: bool
    status: str
    close_at: datetime | None
    created_at: datetime
    platform_message_id: str | None = None
    platform_poll_id: str | None = None
    final_counts: tuple[int, ...] | None = None
    total_voters: int | None = None
    close_failures: int = 0
    lease_token: UUID | None = None
    lease_until: datetime | None = None
    source_action_id: str | None = None
    closed_at: datetime | None = None
