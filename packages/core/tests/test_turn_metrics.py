import json
import logging
from datetime import UTC, datetime

import pytest
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.llm.base import ProviderError
from chatcore.llm.types import Completion, Message, ToolCall, ToolSpec, Usage
from chatcore.pending import PendingAction
from chatcore.plugin import BasePlugin
from chatcore.skill_turn import SkillSettings
from chatcore.skills import Gate, GateState, Skill, SkillsMode, ToolGate
from chatcore.testing import GROUP, FakePlatform, memory_stores, message
from chatcore.tools.registry import Tool, ToolRegistry, ToolResult
from chatcore.turn_metrics import TurnMetrics


async def _handler(*_args, **_kwargs):
    return ToolResult("ok")


def _registry():
    return ToolRegistry(
        [
            Tool("work", "Does work", {"type": "object", "properties": {}}, _handler),
            Tool(
                "admin_work",
                "Admin work",
                {"type": "object", "properties": {}},
                _handler,
                admin_only=True,
            ),
        ]
    )


def test_calls_validate_and_deduplicate_names_and_count_not_offered():
    registry = _registry()
    metrics = TurnMetrics(registry, registry.specs(is_admin=False))

    metrics.note_call("work")
    metrics.note_call("work")
    metrics.note_call("admin_work")
    metrics.note_call("ZQX_SENTINEL_7731_tool")

    assert metrics.called == ["work", "admin_work"]
    assert metrics.not_offered == 1
    assert metrics.unknown_calls == 1


def test_spec_bytes_uses_canonical_spec_json():
    registry = _registry()
    offered = registry.specs(is_admin=False)
    metrics = TurnMetrics(registry, offered)
    payload = [
        {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
        for spec in offered
    ]
    expected = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    assert metrics.offered == len(offered)
    assert metrics.spec_bytes == expected


def test_emit_formats_the_stage_zero_line(caplog):
    registry = _registry()
    metrics = TurnMetrics(registry, registry.specs(is_admin=False))
    metrics.note_call("work")
    metrics.note_stripped()
    metrics.note_retry()

    with caplog.at_level(logging.INFO, logger="chatcore.agent"):
        metrics.emit(
            logging.getLogger("chatcore.agent"),
            chat_key="fake:group",
            steps=2,
            final_forced="steps",
            end="answer",
            usage=Usage(11, 7),
            ms=123.9,
        )

    assert caplog.records[-1].getMessage() == (
        "turn metrics chat=fake:group mode=off offered=1 spec_bytes="
        f"{metrics.spec_bytes} steps=2 called=work unknown_calls=0 not_offered=0 "
        "final_forced=steps markup_stripped=1 retries=1 end=answer "
        "in_tokens=11 out_tokens=7 ms=123 skills=- loaded=- source=- "
        "embed_skipped=- top=- meta_calls=- hints=- route_ms=- eligible_calls=- "
        "unevaluated=- initial_cover=- oracle_cover=- unavailable_routed=- gate_ms=- "
        "model=- vision=-"
    )


def test_emit_appends_the_turn_model_and_vision(caplog):
    registry = _registry()
    metrics = TurnMetrics(registry, registry.specs(is_admin=False))

    with caplog.at_level(logging.INFO, logger="chatcore.agent"):
        metrics.emit(
            logging.getLogger("chatcore.agent"),
            chat_key="fake:group",
            steps=0,
            final_forced="-",
            end="answer",
            usage=Usage(),
            ms=1,
            model="glm-test",
            vision=False,
        )

    assert caplog.records[-1].getMessage().endswith(" gate_ms=- model=glm-test vision=off")


@pytest.mark.parametrize(
    ("final_forced", "end"),
    [("bad", "answer"), ("-", "bad")],
)
def test_emit_rejects_invalid_categories(final_forced, end):
    registry = _registry()
    metrics = TurnMetrics(registry, registry.specs(is_admin=False))
    with pytest.raises(ValueError):
        metrics.emit(
            logging.getLogger("chatcore.agent"),
            chat_key="fake:group",
            steps=0,
            final_forced=final_forced,
            end=end,
            usage=Usage(),
            ms=0,
        )


def _completion(text="answer", calls=(), reason="end_turn", usage=(3, 2)):
    return Completion(Message.assistant(text, tuple(calls)), reason, Usage(*usage))


class _Provider:
    name = "metrics-test"

    def __init__(self, responses=(), fail=False):
        self.responses = list(responses)
        self.fail = fail
        self.calls = []

    async def complete(self, messages, **request):
        self.calls.append({"messages": list(messages), **request})
        if self.fail:
            raise ProviderError("provider sentinel")
        return self.responses.pop(0)


def _agent(
    provider,
    *,
    handler=None,
    steps=8,
    stores=None,
    skill_settings=None,
    extra_plugins=(),
):
    class TestPlugin(BasePlugin):
        name = "metrics_test"

        def tools(self):
            if handler is None:
                return []
            return [
                Tool(
                    "work",
                    "Do test work",
                    {"type": "object", "properties": {"label": {"type": "string"}}},
                    handler,
                )
            ]

        def skills(self):
            return [
                Skill(
                    "metrics_skill",
                    "Metrics skill",
                    ("skill task one", "skill task two", "skill task three", "skill task four"),
                    ("work",),
                    keywords=("skill",),
                )
            ]

        def action_handlers(self):
            class Handler:
                async def commit(self, *_args):
                    return "saved"

            return {"write": Handler()}

    platform = FakePlatform()
    app = BotApp(
        platform,
        stores=stores or memory_stores(),
        access=AccessPolicy([GROUP.key], []),
        bot_name="bot",
        responder=AgentResponder(
            provider,
            system_prompt="rules",
            settings=AgentSettings(
                timeout_s=10,
                final_answer_reserve_s=0.1,
                tool_steps=steps,
                skills=skill_settings or SkillSettings(),
            ),
            limits=type(
                "AllowLimits",
                (),
                {
                    "check": _allow,
                    "record": _record,
                    "budget_exhausted": _no_budget,
                },
            )(),
            clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
        ),
        plugins=[TestPlugin(), *extra_plugins],
    )
    return app, platform


async def _allow(*_args):
    return None


async def _record(*_args):
    return None


async def _no_budget(*_args):
    return False


def _metric_record(caplog):
    records = [record for record in caplog.records if "turn metrics" in record.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].name == "chatcore.agent"
    return records[0].getMessage()


@pytest.mark.parametrize(
    ("provider", "handler", "steps", "expected"),
    [
        (_Provider([_completion(usage=(5, 2))]), None, 8, "steps=0 called=-"),
        (
            _Provider(
                [
                    _completion("", [ToolCall("c1", "work", {"label": "ok"})], "tool_use", (5, 2)),
                    _completion(usage=(7, 3)),
                ]
            ),
            _handler,
            8,
            "steps=1 called=work",
        ),
        (
            _Provider(
                [
                    _completion("", [ToolCall("c1", "work", {"label": "ok"})], "tool_use"),
                    _completion("final"),
                ]
            ),
            _handler,
            1,
            "final_forced=steps",
        ),
        (_Provider(fail=True), None, 0, "end=provider_error"),
        (_Provider([_completion("", reason="refusal")]), None, 0, "end=refusal"),
        (
            _Provider([_completion("", usage=(2, 1)), _completion("retry", usage=(4, 2))]),
            None,
            0,
            "retries=1",
        ),
        (
            _Provider([_completion("clean <｜tool▁calls▁begin｜>")]),
            None,
            0,
            "markup_stripped=1",
        ),
    ],
)
async def test_agent_emits_one_metrics_line(caplog, provider, handler, steps, expected):
    from chatcore.testing import message

    caplog.set_level(logging.DEBUG)
    app, _platform = _agent(provider, handler=handler, steps=steps)
    await app.handle(message("question", mention=True))
    assert expected in _metric_record(caplog)


@pytest.mark.asyncio
async def test_metrics_pending_and_loop_usage_excludes_summary(monkeypatch, caplog):
    from chatcore.conversation import CompactResult, ConversationStore

    captured = []
    original_record_usage = ConversationStore.record_usage

    async def record_usage(self, *args):
        captured.append(args[-2:])
        await original_record_usage(self, *args)

    async def compact(_self, chat, _thread_id, summary_provider, system, _model, **_kwargs):
        completion = await summary_provider.complete(
            [Message.user("summary input")],
            system=system,
            tools=(),
            max_tokens=128,
            session_key=chat.key,
        )
        return CompactResult(completion.usage)

    monkeypatch.setattr(ConversationStore, "record_usage", record_usage)
    monkeypatch.setattr(ConversationStore, "compact", compact)

    async def write(ctx, **_kwargs):
        action = PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin="metrics_test",
            kind="write",
            payload={},
            summary="confirm",
        )
        return ToolResult("pending", pending_action=action)

    provider = _Provider(
        [
            _completion("", [ToolCall("w", "work", {"label": "x"})], "tool_use", (5, 2)),
            _completion("unused", usage=(7, 3)),
        ]
    )
    caplog.set_level(logging.INFO)
    app, _platform = _agent(provider, handler=write)
    await app.handle(message("write", mention=True))
    line = _metric_record(caplog)
    assert "end=pending" in line
    assert "in_tokens=5 out_tokens=2" in line
    assert captured[-1] == (5, 2)

    caplog.clear()
    provider = _Provider(
        [_completion("answer", usage=(6, 4)), _completion("summary", usage=(100, 200))]
    )
    app, _platform = _agent(provider, steps=0)
    await app.handle(message("plain", mention=True))
    assert "in_tokens=6 out_tokens=4" in _metric_record(caplog)
    assert captured[-1] == (106, 204)


@pytest.mark.asyncio
async def test_pending_confirmation_counts_deferred_model_tool_calls(caplog):
    async def write(ctx, **_kwargs):
        action = PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin="metrics_test",
            kind="write",
            payload={},
            summary="confirm",
        )
        return ToolResult("pending", pending_action=action)

    provider = _Provider(
        [
            _completion(
                "",
                [
                    ToolCall("w", "work", {"label": "x"}),
                    ToolCall("u", "ZQX_fake", {}),
                ],
                "tool_use",
            )
        ]
    )
    caplog.set_level(logging.INFO)
    app, _platform = _agent(provider, handler=write)
    await app.handle(message("write", mention=True))

    line = _metric_record(caplog)
    assert "unknown_calls=1" in line
    assert "end=pending" in line


@pytest.mark.asyncio
async def test_logs_do_not_contain_untrusted_sentinel(caplog):
    sentinel = "ZQX_SENTINEL_7731"

    async def raises(_ctx, **_kwargs):
        raise ValueError(sentinel)

    provider = _Provider(
        [
            _completion(
                "",
                [
                    ToolCall("unknown", sentinel + "_tool", {}),
                    ToolCall("badargs", "work", {sentinel: sentinel}),
                    ToolCall("raises", "work", {"label": "raise"}),
                    ToolCall("invalid", "work", {"__invalid_json__": sentinel}),
                ],
                "tool_use",
            ),
            _completion("safe <｜ZQX_SENTINEL_7731｜>"),
        ]
    )
    caplog.set_level(logging.DEBUG)
    app, _platform = _agent(provider, handler=raises)
    await app.handle(message("user says " + sentinel, mention=True))
    for record in caplog.records:
        assert sentinel not in record.getMessage()
        assert sentinel not in str(record.args)
        if record.exc_info:
            import traceback

            assert sentinel not in "".join(traceback.format_exception(*record.exc_info))
    assert "unknown_calls=1" in _metric_record(caplog)


@pytest.mark.parametrize("mode", [SkillsMode.ON, SkillsMode.SHADOW])
async def test_skill_mode_logs_state_and_metrics_exclude_model_sentinels(caplog, mode):
    sentinel = "ZQX_SKILL_PRIVATE_9204"
    provider = _Provider(
        [
            _completion(
                "",
                [
                    ToolCall("meta", "use_skill", {"name": sentinel}),
                    ToolCall("fabricated", sentinel + "_tool", {}),
                    ToolCall("malformed", "use_skill", {"name": sentinel, "extra": sentinel}),
                ],
                "tool_use",
            ),
            _completion("safe reply"),
        ]
    )
    caplog.set_level(logging.DEBUG)
    settings = SkillSettings(
        mode=mode,
        chats=frozenset({GROUP.key}),
        router="keyword",
    )
    app, _platform = _agent(provider, handler=_handler, skill_settings=settings)
    incoming = message("please help " + sentinel, mention=True)
    await app.handle(incoming)
    for record in caplog.records:
        assert sentinel not in record.getMessage()
        assert sentinel not in str(record.args)
        if record.exc_info:
            import traceback

            assert sentinel not in "".join(traceback.format_exception(*record.exc_info))
    metric = _metric_record(caplog)
    assert f"mode={mode.value}" in metric
    assert sentinel not in metric
    rows = await app.responder._store(app).recent(incoming.chat, incoming.thread_id)
    skill_records = [row.content["skill_state"] for row in rows if "skill_state" in row.content]
    assert skill_records
    assert all(sentinel not in json.dumps(item) for item in skill_records)


@pytest.mark.parametrize(
    ("enabled", "text", "toggle_during_call", "expected"),
    [
        (
            True,
            "general question",
            False,
            ("eligible_calls=1", "initial_cover=0/1", "oracle_cover=1/1"),
        ),
        (
            False,
            "poll please",
            False,
            ("eligible_calls=0", "unavailable_routed=1"),
        ),
        (
            True,
            "poll please",
            True,
            ("eligible_calls=1", "initial_cover=1/1", "oracle_cover=1/1"),
        ),
    ],
)
async def test_shadow_coverage_uses_turn_snapshot_not_later_toggle(
    caplog, enabled, text, toggle_during_call, expected
):
    class PollPlugin(BasePlugin):
        name = "metrics_polls"

        def __init__(self):
            self.enabled = enabled
            self.toggle = toggle_during_call

        def tools(self):
            async def create_poll(_ctx):
                if self.toggle:
                    self.enabled = False
                return ToolResult("poll proposed")

            return [
                Tool(
                    "create_poll", "Create poll", {"type": "object", "properties": {}}, create_poll
                )
            ]

        def skills(self):
            return [
                Skill(
                    "polls",
                    "Create polls",
                    ("make a poll", "poll this", "poll options", "poll in chat"),
                    ("create_poll",),
                    keywords=("poll",),
                )
            ]

        def tool_gates(self):
            async def scope(_chat, _is_dm):
                return Gate(
                    GateState.AVAILABLE if self.enabled else GateState.OFF,
                    "Polls off: {cmd:polltools} on.",
                )

            return {"create_poll": ToolGate(scope=scope)}

    plugin = PollPlugin()
    provider = _Provider(
        [
            _completion("", [ToolCall("poll-call", "create_poll", {})], "tool_use"),
            _completion("done"),
        ]
    )
    caplog.set_level(logging.INFO)
    settings = SkillSettings(
        mode=SkillsMode.SHADOW,
        chats=frozenset({GROUP.key}),
        router="keyword",
    )
    app, _platform = _agent(
        provider,
        handler=_handler,
        skill_settings=settings,
        extra_plugins=(plugin,),
    )
    await app.handle(message(text, mention=True))
    metric = _metric_record(caplog)
    for field in expected:
        assert field in metric


@pytest.mark.asyncio
async def test_provider_requests_are_identical_with_metrics_stub(monkeypatch):
    import chatcore.agent as agent_module

    class StubMetrics:
        def __init__(self, *_args, **_kwargs):
            pass

        def note_call(self, *_args):
            pass

        def note_stripped(self):
            pass

        def note_retry(self):
            pass

        def emit(self, *_args, **_kwargs):
            pass

    async def run_scenario(*, steps):
        provider = _Provider(
            [
                _completion("", [ToolCall("c1", "work", {"label": "ok"})], "tool_use"),
                _completion("done"),
            ]
        )
        app, _platform = _agent(provider, handler=_handler, steps=steps)
        await app.handle(incoming)
        normalized = []
        for request in provider.calls:
            normalized.append(
                {
                    "system": request["system"],
                    "tools": request["tools"],
                    "tool_choice": request["tool_choice"],
                    "max_tokens": request["max_tokens"],
                    "session_key": request["session_key"],
                    "messages": [
                        {
                            "role": item.role.value,
                            "text": item.text,
                            "tool_calls": item.tool_calls,
                            "tool_call_id": item.tool_call_id,
                        }
                        for item in request["messages"]
                    ],
                }
            )
        return provider.calls, normalized

    expected_spec = ToolSpec(
        "work",
        "Do test work",
        {"type": "object", "properties": {"label": {"type": "string"}}},
    )
    incoming = message("question", mention=True)
    monkeypatch.setattr(agent_module.secrets, "token_hex", lambda _count: "stable-test-tag")
    for steps, expected_choices in ((8, ["auto", "auto"]), (1, ["auto", "none"])):
        normal_calls, normal = await run_scenario(steps=steps)
        assert [spec.name for spec in normal_calls[0]["tools"]] == ["work", "read_explainer"]
        assert normal_calls[0]["tools"][0] == expected_spec
        assert normal_calls[0]["tool_choice"] == "auto"
        with monkeypatch.context() as metrics_patch:
            metrics_patch.setattr(agent_module, "TurnMetrics", StubMetrics)
            stub_calls, stubbed = await run_scenario(steps=steps)
        assert [call["tool_choice"] for call in normal_calls] == expected_choices
        assert normal == stubbed
