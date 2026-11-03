"""Provider-neutral LLM types. Providers translate these to and from their own wire formats."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str


@dataclass(frozen=True, slots=True)
class ImagePart:
    data: bytes
    media_type: str  # e.g. "image/jpeg"


ContentPart = TextPart | ImagePart


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Message:
    """One conversation entry. A tool result is ``Role.TOOL`` with ``tool_call_id`` set;
    an assistant turn that calls tools carries them in ``tool_calls``."""

    role: Role
    content: tuple[ContentPart, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    is_error: bool = False
    raw: Any = None
    """Provider-specific original content to replay unchanged within the same agent turn (e.g.
    Anthropic thinking blocks). A provider ignores ``raw`` it didn't produce. Never persisted."""
    raw_provider: str | None = None

    @classmethod
    def user(cls, text: str, *images: ImagePart) -> Message:
        return cls(Role.USER, (TextPart(text), *images))

    @classmethod
    def assistant(cls, text: str, tool_calls: tuple[ToolCall, ...] = ()) -> Message:
        return cls(Role.ASSISTANT, (TextPart(text),) if text else (), tool_calls)

    @classmethod
    def tool_result(cls, tool_call_id: str, text: str, *, is_error: bool = False) -> Message:
        return cls(Role.TOOL, (TextPart(text),), tool_call_id=tool_call_id, is_error=is_error)

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.content if isinstance(p, TextPart))


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "error"]
ToolChoice = Literal["auto", "none"]


@dataclass(frozen=True, slots=True)
class Completion:
    message: Message
    stop_reason: StopReason
    usage: Usage = field(default_factory=Usage)
