from __future__ import annotations

import pytest
from chatcore.events import ChatRef, UserRef
from chatcore.llm.types import Completion, Message, ToolCall, Usage
from chatcore_extensions.generator import ExtensionGenerator, GenerationError

# ruff: noqa: E501
from chatcore_extensions.models import ExampleCase


class _Limits:
    def __init__(self):
        self.recorded = []

    async def check(self, chat, user):
        return None

    async def record(self, tokens):
        self.recorded.append(tokens)


class _Provider:
    def __init__(self, completion):
        self.completion = completion
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.completion


@pytest.mark.asyncio
async def test_generation_uses_only_request_confirmed_examples_and_records_usage() -> None:
    provider = _Provider(
        Completion(
            Message.assistant(
                '{"description":"unit conversion","source":"def run(arguments): return arguments[\'x\'] * 2","cases":[{"arguments":{"x":3},"expected":6}]}'
            ),
            "end_turn",
            Usage(10, 20),
        )
    )
    limits = _Limits()
    human = ExampleCase({"x": 1}, 2)
    generator = ExtensionGenerator(provider, limits)
    artifact = await generator.generate(
        chat=ChatRef("tg", "-1"),
        requester=UserRef("tg", "7"),
        description="double an input",
        examples=(human,),
        build_id="build1",
    )

    assert artifact.cases[0] == human
    assert len(artifact.cases) == 1
    assert limits.recorded == [30]
    messages, kwargs = provider.calls[0]
    assert kwargs["tools"] == () and kwargs["max_tokens"] == 4096
    assert "build1" in kwargs["session_key"]
    assert "double an input" in messages[0].text


@pytest.mark.asyncio
async def test_generation_rejects_tool_calls_refusal_invalid_and_large_output() -> None:
    invalid = Completion(Message.assistant("no"), "end_turn")
    with pytest.raises(GenerationError):
        await ExtensionGenerator(_Provider(invalid), _Limits()).generate(
            chat=ChatRef("tg", "-1"),
            requester=UserRef("tg", "7"),
            description="x",
            examples=(ExampleCase({}, 1),),
            build_id="x",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion",
    [
        Completion(Message.assistant("refused"), "refusal", Usage(3, 4)),
        Completion(
            Message.assistant("", (ToolCall("call", "tool", {}),)),
            "tool_use",
            Usage(5, 6),
        ),
        Completion(Message.assistant("truncated"), "max_tokens", Usage(7, 8)),
        Completion(Message.assistant("malformed"), "end_turn", Usage(9, 10)),
    ],
)
async def test_generation_records_returned_usage_once_before_rejecting(completion) -> None:
    limits = _Limits()
    with pytest.raises(GenerationError):
        await ExtensionGenerator(_Provider(completion), limits).generate(
            chat=ChatRef("tg", "-1"),
            requester=UserRef("tg", "7"),
            description="x",
            examples=(ExampleCase({}, 1),),
        )
    assert limits.recorded == [completion.usage.input_tokens + completion.usage.output_tokens]
