from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from chatcore.llm.factory import LLMSettings, build_provider
from chatcore.llm.types import Message
from chatcore.model_choice import (
    ModelChoice,
    ModelOverrideStore,
    build_model_switch,
    parse_model_choices,
)
from chatcore.store.memory import MemoryCache
from chatcore.testing import GROUP

OTHER = GROUP.__class__(GROUP.platform, "other")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def _store(clock, cache=None, **kwargs):
    return ModelOverrideStore(cache or MemoryCache(), clock=clock, **kwargs)


# -- provider view (phase 1) ---------------------------------------------------------------


async def test_with_model_sends_the_new_model_and_keeps_opencode_headers():
    provider = build_provider(
        LLMSettings(
            LLM_BASE_URL="https://opencode.ai/zen/go/v1",
            LLM_MODEL="default-model",
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
    view = provider.with_model("other-model")

    from chatcore.llm.types import ToolSpec

    tool = ToolSpec("lookup", "look up", {"type": "object", "properties": {}})
    await view.complete([Message.user("hi")], system="rules", tools=[tool], session_key="fake:g")
    request = provider._client.chat.completions.create.await_args.kwargs
    assert request["model"] == "other-model"
    assert request["extra_headers"] == {"x-opencode-session": "fake:g"}
    assert request["parallel_tool_calls"] is False
    assert view._client is provider._client
    assert provider.model == "default-model"

    await provider.complete([Message.user("hi")], system="rules")
    assert provider._client.chat.completions.create.await_args.kwargs["model"] == "default-model"


# -- parsing and config (phase 2) ----------------------------------------------------------


def test_parse_model_choices_marks_text_only_entries():
    assert parse_model_choices("") == ()
    assert parse_model_choices("  ") == ()
    assert parse_model_choices("glm-5, kimi-k3:text ,qwen/q4") == (
        ModelChoice("glm-5"),
        ModelChoice("kimi-k3", vision=False),
        ModelChoice("qwen/q4"),
    )


def test_parse_model_choices_accepts_training_marker_in_either_order():
    assert parse_model_choices("a:train,b:text:train,c:TRAIN:text,d:text,e") == (
        ModelChoice("a", trains_on_input=True),
        ModelChoice("b", vision=False, trains_on_input=True),
        ModelChoice("c", vision=False, trains_on_input=True),
        ModelChoice("d", vision=False),
        ModelChoice("e"),
    )


@pytest.mark.parametrize(
    "raw",
    [
        "glm-5,,kimi",
        "glm-5,GLM-5",
        "bad name",
        "default",
        "reset:text",
        "x:vision",
        "x:",
        "x::train",
        "x:text:text",
        "x:train:train",
        "x:Train:train",
        "-lead",
    ],
)
def test_parse_model_choices_rejects_bad_lists(raw):
    with pytest.raises(ValueError):
        parse_model_choices(raw)


def _build(**overrides):
    values = {
        "provider_kind": "openai_compat",
        "default_model": "default-model",
        "choices_raw": "glm-5",
        "cache": MemoryCache(),
        "duration_minutes": 30,
        "max_minutes": 60,
        "cooldown_s": 300,
        "who": "anyone",
    }
    values.update(overrides)
    return build_model_switch(**values)


def test_build_model_switch_is_off_without_choices_and_validates_settings():
    assert _build(choices_raw="") is None
    config = _build(who="admins")
    assert config is not None and config.anyone is False
    assert config.choice("GLM-5") == ModelChoice("glm-5")
    with pytest.raises(ValueError, match="openai_compat"):
        _build(provider_kind="anthropic")
    with pytest.raises(ValueError, match="LLM_MODEL"):
        _build(default_model="")


def test_llm_settings_defaults_keep_model_switch_off():
    settings = LLMSettings()
    assert settings.LLM_MODEL_CHOICES == ""
    assert settings.LLM_MODEL_OVERRIDE_MINUTES == 30
    assert settings.LLM_MODEL_OVERRIDE_MAX_MINUTES == 60
    assert settings.LLM_MODEL_SWITCH_COOLDOWN_S == 300
    assert settings.LLM_MODEL_SWITCH == "anyone"


# -- override store ------------------------------------------------------------------------


async def test_switch_sets_an_override_that_expires():
    clock = Clock()
    store = _store(clock)
    result = await store.switch(GROUP, "glm-5", set_by="Alice")
    assert result.override is not None and result.reason == ""
    assert result.override.until == clock.now + timedelta(minutes=30)
    current = await store.get(GROUP)
    assert current is not None and (current.model, current.set_by) == ("glm-5", "Alice")
    assert await store.get(OTHER) is None

    clock.advance(minutes=30)
    assert await store.get(GROUP) is None


async def test_switching_to_the_active_model_does_not_extend_it():
    clock = Clock()
    store = _store(clock, cooldown_s=0)
    first = await store.switch(GROUP, "glm-5", set_by="Alice")
    clock.advance(minutes=10)
    again = await store.switch(GROUP, "GLM-5", set_by="Bob")
    assert again.reason == "already"
    assert again.override.until == first.override.until
    assert again.override.set_by == "Alice"


async def test_cooldown_blocks_a_second_switch_but_clear_still_works():
    clock = Clock()
    store = _store(clock)
    await store.switch(GROUP, "glm-5", set_by="Alice")
    clock.advance(minutes=1)
    blocked = await store.switch(GROUP, "kimi", set_by="Bob")
    assert blocked.reason == "cooldown"
    assert blocked.retry_at == clock.now + timedelta(minutes=4)

    cleared = await store.clear(GROUP)
    assert cleared is not None and cleared.model == "glm-5"
    assert await store.get(GROUP) is None
    assert (await store.switch(GROUP, "kimi", set_by="Bob")).reason == "cooldown"

    clock.advance(minutes=4)
    assert (await store.switch(GROUP, "kimi", set_by="Bob")).override is not None


async def test_usage_cap_shortens_then_refuses_switches_in_the_rolling_window():
    clock = Clock()
    store = _store(clock, cooldown_s=0)
    await store.switch(GROUP, "glm-5", set_by="Alice")
    clock.advance(minutes=30)  # expired after its full 30 minutes
    second = await store.switch(GROUP, "glm-5", set_by="Alice")
    assert second.override.until == clock.now + timedelta(minutes=30)
    clock.advance(minutes=20)
    # Switching models ends the running one: 50 minutes used, 10 left.
    third = await store.switch(GROUP, "kimi", set_by="Bob")
    assert third.override.until == clock.now + timedelta(minutes=10)
    assert await store.minutes_used(GROUP) == pytest.approx(50)
    clock.advance(minutes=10)

    refused = await store.switch(GROUP, "glm-5", set_by="Alice")
    assert refused.reason == "cap"
    # The first override (12:00-12:30) leaves the window first, from 15:01.
    assert refused.retry_at == datetime(2026, 11, 1, 15, 1, tzinfo=UTC)

    clock.now = refused.retry_at
    assert (await store.switch(GROUP, "glm-5", set_by="Alice")).override is not None


async def test_clear_counts_only_the_time_actually_used():
    clock = Clock()
    store = _store(clock, cooldown_s=0)
    await store.switch(GROUP, "glm-5", set_by="Alice")
    clock.advance(minutes=5)
    await store.clear(GROUP)
    assert await store.minutes_used(GROUP) == pytest.approx(5)
    assert await store.clear(GROUP) is None


async def test_malformed_cache_values_are_ignored():
    clock = Clock()
    cache = MemoryCache()
    store = _store(clock, cache)
    await cache.set(f"model-override:{GROUP.key}", b"not json", ttl_s=60)
    await cache.set(f"model-override-usage:{GROUP.key}", b'[["x", 1], 3]', ttl_s=60)
    await cache.set(f"model-override-cooldown:{GROUP.key}", b"{}", ttl_s=60)
    assert await store.get(GROUP) is None
    assert await store.minutes_used(GROUP) == 0
    assert await store.cooldown_until(GROUP) is None
    assert (await store.switch(GROUP, "glm-5", set_by="Alice")).override is not None


async def test_active_ignores_models_removed_from_the_list():
    clock = Clock()
    cache = MemoryCache()
    config = _build(cache=cache, choices_raw="glm-5,kimi")
    config.store.clock = clock
    await config.store.switch(GROUP, "kimi", set_by="Alice")
    assert (await config.active(GROUP))[1] == ModelChoice("kimi")

    narrowed = _build(cache=cache, choices_raw="glm-5")
    narrowed.store.clock = clock
    assert await narrowed.active(GROUP) is None
