import base64
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from chatcore.llm.base import ProviderError
from chatcore.llm.factory import LLMSettings, build_provider
from chatcore.llm.types import ImagePart, Message, ToolCall, ToolSpec
from openai import BadRequestError, InternalServerError


def test_llm_settings_keep_api_key_secret():
    settings = LLMSettings(LLM_API_KEY="not-a-real-key")
    assert settings.LLM_PROVIDER == "openai_compat"
    assert "not-a-real-key" not in repr(settings)


async def test_openai_compat_translation_and_opencode_session_header():
    provider = build_provider(
        LLMSettings(
            LLM_BASE_URL="https://opencode.ai/zen/go/v1",
            LLM_MODEL="glm-test",
            LLM_API_KEY="fake-key",
        )
    )
    choice = SimpleNamespace(
        message=SimpleNamespace(
            content=None,
            tool_calls=[
                SimpleNamespace(
                    id="call-1",
                    function=SimpleNamespace(name="lookup", arguments='{"x":1}'),
                )
            ],
        ),
        finish_reason="tool_calls",
    )
    provider._client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[choice], usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2)
        )
    )
    image = ImagePart(b"pngbytes", "image/png")
    tool = ToolSpec("lookup", "look up", {"type": "object", "properties": {}})
    result = await provider.complete(
        [Message.user("inspect", image)], system="rules", tools=[tool], session_key="fake:room"
    )
    request = provider._client.chat.completions.create.await_args.kwargs
    assert provider._client.default_headers["User-Agent"] == "sb-bot/1.0"
    assert request["extra_headers"] == {"x-opencode-session": "fake:room"}
    assert request["parallel_tool_calls"] is False
    assert request["messages"][1]["content"][1]["type"] == "image_url"
    assert result.message.tool_calls == (ToolCall("call-1", "lookup", {"x": 1}),)
    assert result.stop_reason == "tool_use"


async def test_opencode_headers_are_not_sent_to_other_hosts():
    provider = build_provider(
        LLMSettings(
            LLM_BASE_URL="https://api.example.test/v1",
            LLM_MODEL="model",
            LLM_API_KEY="fake-key",
        )
    )
    provider._client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=[]), finish_reason="stop"
                )
            ],
            usage=None,
        )
    )
    await provider.complete([Message.user("hi")], system="rules", session_key="fake:room")
    request = provider._client.chat.completions.create.await_args.kwargs
    assert request.get("extra_headers") is None
    assert "parallel_tool_calls" not in request


async def test_openai_tool_choice_none_declares_tools_without_parallel_calls():
    provider = build_provider(
        LLMSettings(
            LLM_BASE_URL="https://opencode.ai/zen/go/v1",
            LLM_MODEL="model",
            LLM_API_KEY="fake-key",
        )
    )
    provider._client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=[]), finish_reason="stop"
                )
            ],
            usage=None,
        )
    )
    tool = ToolSpec("lookup", "look up", {"type": "object", "properties": {}})
    await provider.complete([Message.user("hi")], system="rules", tools=[tool], tool_choice="auto")
    request = provider._client.chat.completions.create.await_args.kwargs
    assert request["parallel_tool_calls"] is False
    assert "tool_choice" not in request
    await provider.complete([Message.user("hi")], system="rules", tools=[tool], tool_choice="none")
    request = provider._client.chat.completions.create.await_args.kwargs
    assert request["tool_choice"] == "none"
    assert request["tools"] and "parallel_tool_calls" not in request
    await provider.complete([Message.user("hi")], system="rules", tools=[tool])
    assert "tool_choice" not in provider._client.chat.completions.create.await_args.kwargs
    await provider.complete([Message.user("hi")], system="rules", tool_choice="none")
    assert "tool_choice" not in provider._client.chat.completions.create.await_args.kwargs


def _openai_tool_choice_error(error_text, *, status=400, body=None):
    response = httpx2.Response(
        status,
        request=httpx2.Request("POST", "https://x/v1/chat/completions"),
    )
    error = BadRequestError if status == 400 else InternalServerError
    return error(error_text, response=response, body=body or {"message": error_text})


def _openai_provider_for_tool_choice_fallback():
    provider = build_provider(
        LLMSettings(
            LLM_BASE_URL="https://opencode.ai/zen/go/v1",
            LLM_MODEL="muse-spark-1.3-contributor",
            LLM_API_KEY="fake-key",
        )
    )
    tool = ToolSpec("lookup", "look up", {"type": "object", "properties": {}})
    return provider, tool


def _openai_reply(text):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=[]), finish_reason="stop"
            )
        ],
        usage=None,
    )


@pytest.mark.parametrize(
    "error_text",
    [
        'Upstream request failed: [invalid_request_error] only "auto" is supported',
        r"Upstream request failed: [invalid_request_error] only \"auto\" is supported",
    ],
)
async def test_openai_tool_choice_none_retries_when_only_auto_is_supported(caplog, error_text):
    provider, tool = _openai_provider_for_tool_choice_fallback()
    provider._client.chat.completions.create = AsyncMock(
        side_effect=[
            _openai_tool_choice_error(error_text, status=400, body=error_text),
            _openai_reply("final reply"),
        ]
    )
    caplog.set_level(logging.INFO, logger="chatcore.llm.openai_compat")

    completion = await provider.complete(
        [Message.user("hi")],
        system="rules",
        tools=[tool],
        tool_choice="none",
        session_key="fake:room",
    )

    create = provider._client.chat.completions.create
    assert create.await_count == 2
    first, retry = (call.kwargs for call in create.await_args_list)
    assert first["tool_choice"] == "none"
    assert retry["model"] == "muse-spark-1.3-contributor"
    assert retry["messages"] == first["messages"]
    assert retry["extra_headers"] == {"x-opencode-session": "fake:room"}
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & retry.keys()
    assert completion.message.text == "final reply"
    assert "tool_choice=none rejected, retrying without tools" in caplog.text
    assert "model=muse-spark-1.3-contributor" in caplog.text


async def test_openai_tool_choice_none_retry_keeps_tool_call_history():
    provider, tool = _openai_provider_for_tool_choice_fallback()
    provider._client.chat.completions.create = AsyncMock(
        side_effect=[
            _openai_tool_choice_error('unsupported "tool_choice"'),
            _openai_reply("final reply"),
        ]
    )
    history = [
        Message.user("look that up"),
        Message.assistant("", (ToolCall("call-1", "lookup", {"query": "example"}),)),
        Message.tool_result("call-1", "found it"),
    ]

    await provider.complete(history, system="rules", tools=[tool], tool_choice="none")

    create = provider._client.chat.completions.create
    assert create.await_count == 2
    first, retry = (call.kwargs for call in create.await_args_list)
    assert retry["messages"] == first["messages"]
    assert retry["messages"][2]["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"query": "example"}'},
        }
    ]
    assert retry["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "found it",
    }
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & retry.keys()


@pytest.mark.parametrize(
    ("error", "tool_choice"),
    [
        (_openai_tool_choice_error("invalid temperature"), "none"),
        (_openai_tool_choice_error("unsupported TOOL_CHOICE"), "auto"),
        (_openai_tool_choice_error("unsupported TOOL_CHOICE", status=500), "none"),
    ],
)
async def test_openai_tool_choice_fallback_only_matches_400_none(error, tool_choice):
    provider, tool = _openai_provider_for_tool_choice_fallback()
    provider._client.chat.completions.create = AsyncMock(side_effect=error)

    with pytest.raises(ProviderError):
        await provider.complete(
            [Message.user("hi")], system="rules", tools=[tool], tool_choice=tool_choice
        )

    assert provider._client.chat.completions.create.await_count == 1


async def test_openai_tool_choice_none_retry_failure_is_not_retried_again():
    provider, tool = _openai_provider_for_tool_choice_fallback()
    provider._client.chat.completions.create = AsyncMock(
        side_effect=[
            _openai_tool_choice_error('unsupported "tool_choice"'),
            _openai_tool_choice_error('unsupported "tool_choice"'),
        ]
    )

    with pytest.raises(ProviderError):
        await provider.complete(
            [Message.user("hi")], system="rules", tools=[tool], tool_choice="none"
        )

    assert provider._client.chat.completions.create.await_count == 2


async def test_openai_tool_choice_none_success_first_try_is_single_request():
    provider, tool = _openai_provider_for_tool_choice_fallback()
    provider._client.chat.completions.create = AsyncMock(return_value=_openai_reply("ok"))

    completion = await provider.complete(
        [Message.user("hi")], system="rules", tools=[tool], tool_choice="none"
    )

    assert provider._client.chat.completions.create.await_count == 1
    assert completion.message.text == "ok"


async def test_anthropic_tool_choice_none_and_no_tools_request():
    provider = build_provider(
        LLMSettings(LLM_PROVIDER="anthropic", LLM_API_KEY="fake-key", LLM_ANTHROPIC_FALLBACKS=False)
    )
    provider._client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )
    )
    tool = ToolSpec("lookup", "look up", {"type": "object", "properties": {}})
    await provider.complete([Message.user("hi")], system="rules", tools=[tool], tool_choice="none")
    assert provider._client.messages.create.await_args.kwargs["tool_choice"] == {"type": "none"}
    await provider.complete([Message.user("hi")], system="rules", tools=[tool])
    assert provider._client.messages.create.await_args.kwargs["tool_choice"] == {"type": "auto"}
    await provider.complete([Message.user("hi")], system="rules")
    assert "tool_choice" not in provider._client.messages.create.await_args.kwargs


async def test_invalid_openai_tool_arguments_become_a_controlled_error_marker():
    provider = build_provider(
        LLMSettings(
            LLM_BASE_URL="https://api.example.test/v1", LLM_MODEL="model", LLM_API_KEY="fake-key"
        )
    )
    provider._client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="call-1",
                                function=SimpleNamespace(name="lookup", arguments="{bad json"),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        )
    )
    completion = await provider.complete([Message.user("hi")], system="rules")
    assert completion.message.tool_calls[0].arguments == {"__invalid_json__": True}


async def test_anthropic_fallback_request_image_and_response(caplog):
    provider = build_provider(LLMSettings(LLM_PROVIDER="anthropic", LLM_API_KEY="fake-key"))
    fallback_block = SimpleNamespace(
        type="fallback", to_dict=lambda: {"type": "fallback", "model": "fallback-id"}
    )
    text_block = SimpleNamespace(
        type="text", text="hello", to_dict=lambda: {"type": "text", "text": "hello"}
    )
    response = SimpleNamespace(
        content=[fallback_block, text_block],
        stop_reason="end_turn",
        model="claude-serving-id",
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            iterations=[SimpleNamespace(type="fallback_message")],
        ),
    )
    provider._client.beta.messages.create = AsyncMock(return_value=response)
    provider._client.messages.create = AsyncMock()
    tool = ToolSpec("lookup", "find", {"type": "object", "properties": {}})
    image = ImagePart(b"pixel", "image/png")
    caplog.set_level(logging.INFO)
    result = await provider.complete([Message.user("inspect", image)], system="rules", tools=[tool])
    request = provider._client.beta.messages.create.await_args.kwargs
    assert request["model"] == "claude-opus-5-5"
    assert "temperature" not in request and "top_p" not in request and "top_k" not in request
    assert "thinking" not in request
    assert request["tool_choice"] == {"type": "auto"}
    assert request["output_config"] == {"effort": "low"}
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["fallbacks"] == "default"
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    image_block = request["messages"][0]["content"][1]
    assert image_block == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(b"pixel").decode(),
        },
    }
    assert result.message.text == "hello"
    assert result.message.raw_provider == "anthropic"
    assert result.message.raw == [
        {"type": "fallback", "model": "fallback-id"},
        {"type": "text", "text": "hello"},
    ]
    assert "model=claude-serving-id" in caplog.text
    provider._client.messages.create.assert_not_awaited()


async def test_anthropic_replays_raw_assistant_blocks_unchanged():
    provider = build_provider(
        LLMSettings(LLM_PROVIDER="anthropic", LLM_API_KEY="fake-key", LLM_ANTHROPIC_FALLBACKS=False)
    )
    provider._client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
    )
    provider._client.beta.messages.create = AsyncMock()
    raw = [{"type": "thinking", "thinking": "opaque", "signature": "fake-signature"}]
    assistant = Message.assistant("thinking", (ToolCall("tool-1", "lookup", {}),))
    from dataclasses import replace

    assistant = replace(assistant, raw=raw, raw_provider="anthropic")
    await provider.complete([assistant], system="rules")
    request = provider._client.messages.create.await_args.kwargs
    assert request["messages"][0]["content"] is raw
    assert provider._client.beta.messages.create.await_count == 0
    assert "betas" not in request and "fallbacks" not in request


async def test_anthropic_fallbacks_disabled_uses_standard_messages_endpoint():
    provider = build_provider(
        LLMSettings(LLM_PROVIDER="anthropic", LLM_API_KEY="fake-key", LLM_ANTHROPIC_FALLBACKS=False)
    )
    provider._client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[],
            stop_reason="end_turn",
            model="claude-test",
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )
    )
    provider._client.beta.messages.create = AsyncMock()
    await provider.complete([Message.user("hi")], system="rules")
    request = provider._client.messages.create.await_args.kwargs
    assert provider._client.beta.messages.create.await_count == 0
    assert "betas" not in request and "fallbacks" not in request


async def test_anthropic_merges_consecutive_tool_results_into_one_user_message():
    provider = build_provider(
        LLMSettings(LLM_PROVIDER="anthropic", LLM_API_KEY="fake-key", LLM_ANTHROPIC_FALLBACKS=False)
    )
    provider._client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[],
            stop_reason="end_turn",
            model="claude-test",
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )
    )
    messages = [
        Message.user("hi"),
        Message.assistant("", (ToolCall("t1", "one", {}), ToolCall("t2", "two", {}))),
        Message.tool_result("t1", "first"),
        Message.tool_result("t2", "second"),
    ]
    await provider.complete(messages, system="rules")
    request_messages = provider._client.messages.create.await_args.kwargs["messages"]
    assert [item["role"] for item in request_messages] == ["user", "assistant", "user"]
    assert [block["content"] for block in request_messages[-1]["content"]] == ["first", "second"]


async def test_anthropic_refusal_stop_reason_is_preserved():
    provider = build_provider(
        LLMSettings(LLM_PROVIDER="anthropic", LLM_API_KEY="fake-key", LLM_ANTHROPIC_FALLBACKS=False)
    )
    provider._client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[],
            stop_reason="refusal",
            model="claude-test",
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )
    )
    result = await provider.complete([Message.user("hi")], system="rules")
    assert result.stop_reason == "refusal"
