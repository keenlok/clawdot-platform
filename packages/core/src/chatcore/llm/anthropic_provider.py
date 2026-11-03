"""Native Anthropic Messages API transport."""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence

from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    RateLimitError,
)
from anthropic import (
    APIError as AnthropicAPIError,
)

from chatcore.llm.base import ProviderError
from chatcore.llm.types import (
    Completion,
    ImagePart,
    Message,
    Role,
    TextPart,
    ToolCall,
    ToolChoice,
    ToolSpec,
    Usage,
)

log = logging.getLogger(__name__)


def _anthropic_content(message: Message, replay_raw: bool) -> list[dict]:
    if replay_raw and message.raw_provider == "anthropic" and message.raw is not None:
        return message.raw
    result = []
    for part in message.content:
        if isinstance(part, TextPart):
            result.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            result.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.media_type,
                        "data": base64.b64encode(part.data).decode("ascii"),
                    },
                }
            )
    result.extend(
        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
        for c in message.tool_calls
    )
    return result


def _messages(messages: Sequence[Message]) -> list[dict]:
    result: list[dict] = []
    for message in messages:
        if message.role is Role.TOOL:
            tool_result = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.text,
                "is_error": message.is_error,
            }
            if result and result[-1]["role"] == "user" and result[-1].get("_tool_results"):
                result[-1]["content"].append(tool_result)
            else:
                result.append({"role": "user", "content": [tool_result], "_tool_results": True})
        else:
            result.append(
                {
                    "role": message.role.value,
                    "content": _anthropic_content(message, replay_raw=True),
                }
            )
            if result[-1]["role"] == "user":
                result[-1]["_tool_results"] = False
    for item in result:
        item.pop("_tool_results", None)
    return result


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self, *, api_key: str, model: str, timeout: float, effort: str, fallbacks: bool = True
    ) -> None:
        self.model, self.effort = model, effort
        self.fallbacks = fallbacks
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout)

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        system: str,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_tokens: int = 2048,
        session_key: str | None = None,
    ) -> Completion:
        request: dict = {
            "model": self.model,
            "system": system,
            "messages": _messages(messages),
            "max_tokens": max_tokens,
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            request["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
            request["tool_choice"] = {"type": tool_choice}
        try:
            if self.fallbacks:
                response = await self._client.beta.messages.create(
                    **request,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            else:
                response = await self._client.messages.create(**request)
        except RateLimitError as exc:
            log.warning("LLM request failed provider=anthropic error=%s", type(exc).__name__)
            raise ProviderError("Anthropic provider request failed") from None
        except APIStatusError as exc:
            log.warning(
                "LLM request failed provider=anthropic error=%s status=%s",
                type(exc).__name__,
                exc.status_code,
            )
            raise ProviderError("Anthropic provider request failed") from None
        except APIConnectionError as exc:
            log.warning("LLM request failed provider=anthropic error=%s", type(exc).__name__)
            raise ProviderError("Anthropic provider request failed") from None
        except AnthropicAPIError as exc:
            log.warning("LLM request failed provider=anthropic error=%s", type(exc).__name__)
            raise ProviderError("Anthropic provider request failed") from None
        iterations = getattr(getattr(response, "usage", None), "iterations", None) or []
        if any(getattr(item, "type", None) == "fallback_message" for item in iterations):
            log.info("Anthropic fallback served reply model=%s", response.model)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        calls = tuple(
            ToolCall(block.id, block.name, block.input)
            for block in response.content
            if getattr(block, "type", None) == "tool_use"
        )
        reason = {
            "end_turn": "end_turn",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
            "refusal": "refusal",
        }.get(response.stop_reason, "error")
        message = Message(
            Role.ASSISTANT,
            (TextPart(text),) if text else (),
            calls,
            raw=[block.to_dict() for block in response.content],
            raw_provider="anthropic",
        )
        return Completion(
            message, reason, Usage(response.usage.input_tokens, response.usage.output_tokens)
        )
