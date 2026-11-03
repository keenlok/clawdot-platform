"""Language tutor profile model."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TutorProfile:
    user_key: str
    target: str
    level: str
    explain: tuple[str, ...]
    active: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TutorTopic:
    chat_key: str
    thread_key: str
    target: str | None
    level: str | None
    explain: tuple[str, ...] | None
    auto_reply: bool
    set_by: str
    updated_at: datetime
