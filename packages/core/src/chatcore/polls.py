"""Optional native-poll capabilities, independent of the required platform contract."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from chatcore.events import ChatRef, MessageRef, PollAnswer
from chatcore.store.base import Stores


@dataclass(frozen=True, slots=True)
class PollLimits:
    max_options: int
    max_question_len: int
    max_option_len: int


@dataclass(frozen=True, slots=True)
class SentPoll:
    message: MessageRef
    poll_id: str


@dataclass(frozen=True, slots=True)
class PollResult:
    counts: tuple[int, ...]
    total_voters: int


@runtime_checkable
class PollPlatform(Protocol):
    poll_limits: PollLimits

    async def send_poll(
        self,
        chat: ChatRef,
        question: str,
        options: Sequence[str],
        *,
        multiple: bool,
        anonymous: bool,
        thread_id: str | None = None,
    ) -> SentPoll:
        """Send a native poll; raise PlatformError containing only the error class name."""
        ...

    async def stop_poll(self, message: MessageRef) -> PollResult:
        """Stop a bot-sent poll; raise PlatformError containing only the error class name."""
        ...


@runtime_checkable
class PollAnswerListener(Protocol):
    async def on_poll_answer(self, answer: PollAnswer, stores: Stores) -> None: ...
