import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.limits import Limits, LimitsSettings
from chatcore.llm.base import ProviderError
from chatcore.llm.types import Completion, Message, ToolCall, Usage
from chatcore.plugin import BasePlugin
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolResult


class WorkPlugin(BasePlugin):
    name = "budget_test"

    def tools(self):
        async def work(ctx):
            return ToolResult("worked")

        return [Tool("work", "Work", {"type": "object", "properties": {}}, work)]


def _completion(text="", *, calls=(), reason="end_turn", tokens=10):
    return Completion(Message.assistant(text, tuple(calls)), reason, Usage(tokens, 0))


async def _run(provider, *, platform=None, steps=8, prepare=None, limits=None):
    stores = memory_stores()
    limits = limits or Limits(stores.cache, LimitsSettings(DAILY_TOKEN_BUDGET=100_000))
    stores.cache = limits.cache
    responder = AgentResponder(
        provider, system_prompt="", settings=AgentSettings(tool_steps=steps), limits=limits
    )
    app = BotApp(
        platform or FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[WorkPlugin()],
        responder=responder,
        bot_name="bot",
    )
    incoming = message("budget request", mention=True)
    store = responder._store(app)
    usage_rows = []

    async def record_usage(*args):
        usage_rows.append(args)

    store.record_usage = record_usage
    if prepare:
        await prepare(store)
    await responder.respond(incoming, incoming.text, app)
    return app, limits, responder, incoming, store, usage_rows


async def test_budget_crossing_forces_next_round_final_and_records_each_completion(caplog):
    provider = ScriptedProvider(
        [
            _completion(calls=[ToolCall("c1", "work", {})], reason="tool_use", tokens=100_000),
            _completion("answer", tokens=7),
        ]
    )
    with caplog.at_level(logging.INFO, logger="chatcore.agent"):
        app, limits, responder, incoming, _store, usage_rows = await _run(provider)
    assert len(provider.calls) == 2
    assert provider.calls[-1]["tool_choice"] == "none"
    assert "The tool budget is exhausted" in provider.calls[-1]["system"]
    assert app.platform.texts == ["answer"]
    assert await limits.budget_used() == 100_007
    assert usage_rows[0][-2:] == (100_007, 0)
    assert "final_forced=budget" in caplog.text


async def test_budget_crossing_skips_empty_answer_retry():
    provider = ScriptedProvider(
        [
            _completion(calls=[ToolCall("c1", "work", {})], reason="tool_use", tokens=100_000),
            _completion("", tokens=10),
        ]
    )
    app, _limits, _responder, _incoming, _store, _rows = await _run(provider)
    assert len(provider.calls) == 2
    assert app.platform.texts[0].startswith("Sorry, I couldn't finish an answer this time.")


async def test_budget_record_failure_does_not_break_turn(caplog):
    stores = memory_stores()

    class BrokenLimits:
        async def check(self, *_args):
            return None

        async def budget_exhausted(self):
            return False

        async def record(self, _tokens):
            raise RuntimeError("private detail")

    provider = ScriptedProvider([_completion("answer")])
    responder = AgentResponder(
        provider, system_prompt="", settings=AgentSettings(), limits=BrokenLimits()
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        responder=responder,
        bot_name="bot",
    )
    await responder.respond(message("budget request", mention=True), "budget request", app)
    assert app.platform.texts == ["answer"]
    assert "budget record failed" in caplog.text
    assert "private detail" not in caplog.text


async def _add_old_turns(store, count=31):
    for index in range(count):
        await store.add(GROUP, None, "user", {"text": f"old turn {index}"})


async def test_budget_crossed_before_summary_keeps_previous_summary():
    provider = ScriptedProvider([_completion("answer", tokens=10)])

    async def prepare(store):
        await _add_old_turns(store)
        await store.save_summary(GROUP, None, "previous summary", 0)

    limits = Limits(memory_stores().cache, LimitsSettings(DAILY_TOKEN_BUDGET=100_000))
    await limits.record(99_990)
    app, limits, _responder, _incoming, store, usage_rows = await _run(
        provider, steps=0, prepare=prepare, limits=limits
    )
    assert len(provider.calls) == 1
    assert await store.summary(GROUP, None) == ("previous summary", 0)
    assert await limits.budget_used() == 100_000
    assert usage_rows[0][-2:] == (10, 0)
    assert app.platform.texts == ["answer"]


async def test_summary_internal_retry_is_admitted_and_skipped_when_budget_crosses():
    provider = ScriptedProvider(
        [
            _completion("answer", tokens=60_000),
            _completion("", reason="max_tokens", tokens=50_001),
        ]
    )

    async def prepare(store):
        await _add_old_turns(store)
        await store.save_summary(GROUP, None, "previous summary", 0)

    limits = Limits(memory_stores().cache, LimitsSettings(DAILY_TOKEN_BUDGET=100_000))
    await limits.record(30_000)
    _app, limits, _responder, _incoming, store, usage_rows = await _run(
        provider, steps=0, prepare=prepare, limits=limits
    )
    assert len(provider.calls) == 2
    assert await store.summary(GROUP, None) == ("previous summary", 0)
    assert await limits.budget_used() == 140_001
    assert usage_rows[0][-2:] == (110_001, 0)


async def test_usage_total_includes_round_retry_and_summary():
    provider = ScriptedProvider(
        [
            _completion("", tokens=5),
            _completion("answer", tokens=7),
            _completion("summary", tokens=11),
        ]
    )

    async def prepare(store):
        await _add_old_turns(store)

    limits = Limits(memory_stores().cache, LimitsSettings(DAILY_TOKEN_BUDGET=100_000))
    _app, limits, _responder, _incoming, _store, usage_rows = await _run(
        provider, steps=0, prepare=prepare, limits=limits
    )
    assert len(provider.calls) == 3
    assert await limits.budget_used() == 23
    assert len(usage_rows) == 1
    assert usage_rows[0][-2:] == (23, 0)


async def test_send_failure_still_records_usage_and_propagates():
    class BrokenSend(FakePlatform):
        async def send(self, *args, **kwargs):
            raise OSError("sentinel-private-send")

    provider = ScriptedProvider([_completion("answer", tokens=9)])
    limits = Limits(memory_stores().cache, LimitsSettings(DAILY_TOKEN_BUDGET=100_000))
    records = []

    async def capture(store):
        async def record_usage(*args):
            records.append(args)

        store.record_usage = record_usage

    with pytest.raises(OSError, match="sentinel-private-send"):
        await _run(provider, platform=BrokenSend(), limits=limits, prepare=capture)
    assert await limits.budget_used() == 9
    assert len(records) == 1 and records[0][-2:] == (9, 0)


async def test_usage_row_failure_does_not_mask_turn_failure(caplog):
    class BrokenSend(FakePlatform):
        async def send(self, *args, **kwargs):
            raise OSError("original-send-error")

    async def prepare(store):
        async def record_usage(*_args):
            raise RuntimeError("user-sentinel-secret")

        store.record_usage = record_usage

    limits = Limits(memory_stores().cache, LimitsSettings(DAILY_TOKEN_BUDGET=100_000))
    with pytest.raises(OSError, match="original-send-error"):
        await _run(
            ScriptedProvider([_completion("answer", tokens=9)]),
            platform=BrokenSend(),
            limits=limits,
            prepare=prepare,
        )
    assert await limits.budget_used() == 9
    assert "usage record failed" in caplog.text
    assert "user-sentinel-secret" not in caplog.text


async def test_provider_error_mid_turn_records_completed_usage_once():
    class FailsAfterFirst:
        name = "provider-error"

        def __init__(self):
            self.calls = 0

        async def complete(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return _completion(calls=[ToolCall("c1", "work", {})], reason="tool_use", tokens=13)
            raise ProviderError("sentinel-provider-error")

    app, limits, _responder, _incoming, _store, usage_rows = await _run(FailsAfterFirst())
    assert app.platform.texts == [
        "Sorry, I couldn't reach the language model. Please try again later."
    ]
    assert await limits.budget_used() == 13
    assert len(usage_rows) == 1
    assert usage_rows[0][-2:] == (13, 0)


async def test_concurrent_admission_observes_another_turns_record():
    limits = Limits(memory_stores().cache, LimitsSettings(DAILY_TOKEN_BUDGET=100_000))
    b_first_started = asyncio.Event()
    release_b = asyncio.Event()

    class ObserveProvider:
        name = "observe-budget"

        def __init__(self):
            self.calls = []

        async def complete(self, _messages, **request):
            self.calls.append(request)
            if request["session_key"] == GROUP.key:
                return _completion(
                    calls=[ToolCall("a1", "work", {})], reason="tool_use", tokens=100_000
                )
            if request["session_key"] == "fake:b" and request["tool_choice"] == "auto":
                b_first_started.set()
                await release_b.wait()
                return _completion(calls=[ToolCall("b1", "work", {})], reason="tool_use", tokens=0)
            return _completion("final", tokens=3)

    stores = memory_stores()
    stores.cache = limits.cache
    provider = ObserveProvider()
    responder = AgentResponder(
        provider,
        system_prompt="",
        settings=AgentSettings(tool_steps=2),
        limits=limits,
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key, "fake:b"], []),
        plugins=[WorkPlugin()],
        responder=responder,
        bot_name="bot",
    )
    msg_a = message("turn a", chat=GROUP, mention=True)
    from chatcore.events import ChatRef

    msg_b = message("turn b", chat=ChatRef("fake", "b"), mention=True)
    task_b = asyncio.create_task(responder.respond(msg_b, msg_b.text, app))
    await b_first_started.wait()
    await responder.respond(msg_a, msg_a.text, app)
    release_b.set()
    await task_b
    b_requests = [call for call in provider.calls if call["session_key"] == "fake:b"]
    assert len(b_requests) == 2
    assert b_requests[-1]["tool_choice"] == "none"


async def test_budget_barrier_allows_in_flight_completions_and_one_final_per_turn():
    limits = Limits(memory_stores().cache, LimitsSettings(DAILY_TOKEN_BUDGET=100_000))
    stores = memory_stores()
    stores.cache = limits.cache
    both_second_admitted = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()
    second_count = 0

    class BarrierProvider:
        name = "barrier"

        async def complete(self, _messages, **request):
            nonlocal second_count
            key = request["session_key"]
            prior = sum(call["session_key"] == key for call in self.calls)
            self.calls.append(request)
            if prior == 0:
                return _completion(
                    calls=[ToolCall(f"{key}1", "work", {})], reason="tool_use", tokens=40_000
                )
            if prior == 1:
                second_count += 1
                if second_count == 2:
                    both_second_admitted.set()
                await (release_a if key == GROUP.key else release_b).wait()
                tokens = 30_000 if key == GROUP.key else 20_000
                return _completion(
                    calls=[ToolCall(f"{key}2", "work", {})], reason="tool_use", tokens=tokens
                )
            return _completion("final", tokens=5_000)

        def __init__(self):
            self.calls = []

    provider = BarrierProvider()
    responder = AgentResponder(
        provider,
        system_prompt="",
        settings=AgentSettings(tool_steps=3),
        limits=limits,
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key, "fake:b"], []),
        plugins=[WorkPlugin()],
        responder=responder,
        bot_name="bot",
    )
    from chatcore.events import ChatRef

    msg_a = message("turn a", chat=GROUP, mention=True)
    msg_b = message("turn b", chat=ChatRef("fake", "b"), mention=True)
    tasks = [
        asyncio.create_task(responder.respond(msg_a, msg_a.text, app)),
        asyncio.create_task(responder.respond(msg_b, msg_b.text, app)),
    ]
    await asyncio.wait_for(both_second_admitted.wait(), 2)
    release_a.set()
    deadline = asyncio.get_running_loop().time() + 2
    while await limits.budget_used() < 110_000:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.001)
    release_b.set()
    await asyncio.gather(*tasks)
    for key in (GROUP.key, "fake:b"):
        calls = [call for call in provider.calls if call["session_key"] == key]
        assert len(calls) == 3
        assert [call["tool_choice"] for call in calls] == ["auto", "auto", "none"]
    cap = limits.settings.DAILY_TOKEN_BUDGET
    first_rounds = 40_000 + 40_000
    crossing_excess = 30_000 - (cap - first_rounds)
    other_in_flight = 20_000
    one_final_each = 5_000 * 2
    assert await limits.budget_used() == cap + crossing_excess + other_in_flight + one_final_each


async def test_limits_record_warning_does_not_log_sentinel_text(caplog):
    stores = memory_stores()

    class BrokenLimits:
        async def check(self, *_args):
            return None

        async def budget_exhausted(self):
            return False

        async def record(self, _tokens):
            raise RuntimeError("user-sentinel-secret")

    provider = ScriptedProvider([_completion("answer")])
    responder = AgentResponder(
        provider, system_prompt="", settings=AgentSettings(), limits=BrokenLimits()
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        responder=responder,
        bot_name="bot",
    )
    await responder.respond(message("safe question"), "safe question", app)
    assert app.platform.texts == ["answer"]
    assert "user-sentinel-secret" not in caplog.text


async def test_budget_record_finishes_after_turn_timeout_while_waiting_for_write_lock():
    stores = memory_stores()
    limits = Limits(stores.cache, LimitsSettings(DAILY_TOKEN_BUDGET=100_000))
    budget_key = f"budget:{limits._day()}"
    write_key = f"{budget_key}:write"
    held_lock = stores.cache.lock(write_key, ttl_s=10)
    await held_lock.__aenter__()
    original_lock = stores.cache.lock
    record_waiting = asyncio.Event()
    record_finished = asyncio.Event()

    @asynccontextmanager
    async def monitored_lock(key, *, ttl_s):
        if key == write_key:
            record_waiting.set()
        async with original_lock(key, ttl_s=ttl_s):
            yield

    stores.cache.lock = monitored_lock
    original_set = stores.cache.set

    async def monitored_set(key, value, *, ttl_s):
        await original_set(key, value, ttl_s=ttl_s)
        if key == budget_key:
            record_finished.set()

    stores.cache.set = monitored_set
    provider = ScriptedProvider(
        [_completion(calls=[ToolCall("blocked", "work", {})], reason="tool_use", tokens=17)]
    )
    responder = AgentResponder(
        provider,
        system_prompt="",
        settings=AgentSettings(timeout_s=0.15, final_answer_reserve_s=0.05, tool_steps=2),
        limits=limits,
    )
    app = BotApp(
        FakePlatform(),
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[WorkPlugin()],
        responder=responder,
        bot_name="bot",
    )
    usage_rows = []
    conversation_store = responder._store(app)

    async def record_usage(*args):
        usage_rows.append(args[-2:])

    conversation_store.record_usage = record_usage
    turn = asyncio.create_task(
        responder.respond(message("slow budget write", mention=True), "slow budget write", app)
    )
    try:
        await asyncio.wait_for(record_waiting.wait(), timeout=1)
        await asyncio.wait_for(turn, timeout=1)
        assert usage_rows == [(17, 0)]
    finally:
        await held_lock.__aexit__(None, None, None)
    await asyncio.wait_for(record_finished.wait(), timeout=1)
    assert await limits.budget_used() == 17
