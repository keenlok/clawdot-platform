"""OpenAI Chat Completions transport for OpenAI-compatible services."""

from __future__ import annotations

import base64
import copy
import json
import logging
from collections.abc import Sequence
from urllib.parse import urlparse

from openai import APIError as OpenAIAPIError
from openai import AsyncOpenAI, BadRequestError

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


def _rejects_tool_choice_none(error: OpenAIAPIError) -> bool:
    if not isinstance(error, BadRequestError) or error.status_code != 400:
        return False
    message = getattr(error, "message", "")
    body = error.body
    body_text = str(body) if isinstance(body, (dict, str)) else ""
    error_text = f"{message} {body_text}".casefold().replace(r"\"", '"')
    return "tool_choice" in error_text or 'only "auto" is supported' in error_text


def _content(message: Message) -> str | list[dict]:
    if not any(isinstance(part, ImagePart) for part in message.content):
        return message.text
    parts: list[dict] = []
    for part in message.content:
        if isinstance(part, TextPart):
            parts.append({"type": "text", "text": part.text})
        else:
            encoded = base64.b64encode(part.data).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{part.media_type};base64,{encoded}"},
                }
            )
    return parts


def _messages(messages: Sequence[Message]) -> list[dict]:
    result: list[dict] = []
    for message in messages:
        if message.role is Role.TOOL:
            result.append(
                {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.text}
            )
            continue
        item = {"role": message.role.value, "content": _content(message)}
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in message.tool_calls
            ]
        result.append(item)
    return result


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, *, api_key: str, base_url: str | None, model: str, timeout: float) -> None:
        self.model = model
        self._opencode = bool(
            base_url and (urlparse(base_url).hostname or "").lower() == "opencode.ai"
        )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            default_headers={"User-Agent": "sb-bot/1.0"},
        )

    def with_model(self, model: str) -> OpenAICompatProvider:
        """A view of this provider that sends ``model`` instead, sharing the client, the endpoint
        and the OpenCode header logic (the per-chat ``/model`` override)."""
        view = copy.copy(self)
        view.model = model
        return view

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
            "messages": [{"role": "system", "content": system}, *_messages(messages)],
            "max_tokens": max_tokens,
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            if tool_choice == "none":
                request["tool_choice"] = "none"
        headers = {}
        if self._opencode:
            if tools and tool_choice == "auto":
                request["parallel_tool_calls"] = False
            if session_key:
                headers["x-opencode-session"] = session_key
        request = {k: v for k, v in request.items() if v is not None}
        try:
            response = await self._client.chat.completions.create(
                **request, extra_headers=headers or None
            )
        except OpenAIAPIError as exc:
            if not (tools and tool_choice == "none" and _rejects_tool_choice_none(exc)):
                log.warning(
                    "LLM request failed provider=openai_compat error=%s", type(exc).__name__
                )
                raise ProviderError("OpenAI-compatible provider request failed") from None
            log.info("tool_choice=none rejected, retrying without tools model=%s", self.model)
            retry_request = {
                key: value
                for key, value in request.items()
                if key not in {"tools", "tool_choice", "parallel_tool_calls"}
            }
            try:
                response = await self._client.chat.completions.create(
                    **retry_request, extra_headers=headers or None
                )
            except OpenAIAPIError as retry_exc:
                log.warning(
                    "LLM request failed provider=openai_compat error=%s", type(retry_exc).__name__
                )
                raise ProviderError("OpenAI-compatible provider request failed") from None
        if not response.choices:
            raise ProviderError("OpenAI-compatible provider returned no choices")
        choice = response.choices[0]
        calls = []
        for call in choice.message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError
            except (ValueError, TypeError):
                arguments = {"__invalid_json__": True}
            calls.append(ToolCall(call.id, call.function.name, arguments))
        reason = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}.get(
            choice.finish_reason, "error"
        )
        usage = (
            Usage(response.usage.prompt_tokens, response.usage.completion_tokens)
            if response.usage
            else Usage()
        )
        return Completion(
            Message.assistant(choice.message.content or "", tuple(calls)), reason, usage
        )
