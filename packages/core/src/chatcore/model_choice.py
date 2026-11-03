"""Per-chat temporary model override for ``/model`` (``plans/model-switch.md``).

Only the ``/model`` command writes an override; the LLM has no tool for it. Overrides live in the
cache with a TTL, so a restart or a cache flush falls back to the configured default model.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from chatcore.events import ChatRef
from chatcore.store.base import Cache

log = logging.getLogger(__name__)

USAGE_WINDOW = timedelta(hours=3)
"""The rolling window the ``LLM_MODEL_OVERRIDE_MAX_MINUTES`` cap applies to."""

_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
_RESERVED = frozenset({"default", "reset"})


@dataclass(frozen=True, slots=True)
class ModelChoice:
    name: str
    vision: bool = True
    """False for ``name:text`` entries: turns on this model run with vision off."""
    trains_on_input: bool = False
    """True for ``name:train`` entries: the provider trains on prompts and replies."""


def parse_model_choices(raw: str) -> tuple[ModelChoice, ...]:
    """Parse ``LLM_MODEL_CHOICES`` (``a,b:text,c:train``). Bad entries fail at startup."""
    choices: list[ModelChoice] = []
    seen: set[str] = set()
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            if raw.strip():
                raise ValueError("LLM_MODEL_CHOICES has an empty entry")
            continue
        parts = entry.split(":")
        name, markers = parts[0], parts[1:]
        if not _MODEL_NAME.fullmatch(name):
            raise ValueError(f"LLM_MODEL_CHOICES has an invalid model name: {entry!r}")
        parsed_markers: set[str] = set()
        for marker in markers:
            normalized = marker.casefold()
            if normalized not in {"text", "train"}:
                raise ValueError(f"LLM_MODEL_CHOICES has an unknown marker in {entry!r}")
            if normalized in parsed_markers:
                raise ValueError(f"LLM_MODEL_CHOICES repeats a marker in {entry!r}")
            parsed_markers.add(normalized)
        key = name.casefold()
        if key in _RESERVED:
            raise ValueError(f"LLM_MODEL_CHOICES can't use the reserved name {name!r}")
        if key in seen:
            raise ValueError(f"LLM_MODEL_CHOICES lists {name!r} twice")
        seen.add(key)
        choices.append(
            ModelChoice(
                name, vision="text" not in parsed_markers, trains_on_input="train" in parsed_markers
            )
        )
    return tuple(choices)


def find_choice(choices: Sequence[ModelChoice], name: str) -> ModelChoice | None:
    key = name.strip().casefold()
    return next((choice for choice in choices if choice.name.casefold() == key), None)


@dataclass(frozen=True, slots=True)
class ModelOverride:
    model: str
    set_by: str
    """Display name of the person who switched; shown in replies, never logged."""
    until: datetime


@dataclass(frozen=True, slots=True)
class SwitchResult:
    override: ModelOverride | None = None
    """Set when the switch happened."""
    retry_at: datetime | None = None
    """Set when the cooldown or the usage cap refused it."""
    reason: str = ""
    """``cooldown``, ``cap`` or ``already``."""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _ttl(until: datetime, now: datetime) -> int:
    return max(1, math.ceil((until - now).total_seconds()))


class ModelOverrideStore:
    """Cache-backed override, cooldown and usage record for one chat at a time.

    Keys: ``model-override:<chat>`` (the override), ``model-override-cooldown:<chat>`` (when the
    next switch is allowed) and ``model-override-usage:<chat>`` (override intervals in the last
    ``USAGE_WINDOW``, for the renewal cap)."""

    def __init__(
        self,
        cache: Cache,
        *,
        duration_minutes: int = 30,
        max_minutes: int = 60,
        cooldown_s: int = 300,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if duration_minutes < 1 or max_minutes < 1 or cooldown_s < 0:
            raise ValueError("model override durations must be positive")
        self.cache = cache
        self.duration = timedelta(minutes=duration_minutes)
        self.max_used = timedelta(minutes=max_minutes)
        self.cooldown = timedelta(seconds=cooldown_s)
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _key(kind: str, chat: ChatRef) -> str:
        return f"model-override{kind}:{chat.key}"

    async def _read_json(self, key: str) -> object:
        raw = await self.cache.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None

    async def get(self, chat: ChatRef) -> ModelOverride | None:
        data = await self._read_json(self._key("", chat))
        if not isinstance(data, dict):
            return None
        model, set_by = data.get("model"), data.get("set_by")
        until = _parse_time(data.get("until"))
        if not isinstance(model, str) or not isinstance(set_by, str) or until is None:
            return None
        if until <= self.clock():
            return None
        return ModelOverride(model, set_by, until)

    async def _intervals(self, chat: ChatRef) -> list[tuple[datetime, datetime]]:
        data = await self._read_json(self._key("-usage", chat))
        intervals: list[tuple[datetime, datetime]] = []
        if not isinstance(data, list):
            return intervals
        for item in data:
            if not isinstance(item, list) or len(item) != 2:
                continue
            start, end = _parse_time(item[0]), _parse_time(item[1])
            if start is not None and end is not None and start <= end:
                intervals.append((start, end))
        return intervals

    async def _save_intervals(
        self, chat: ChatRef, intervals: list[tuple[datetime, datetime]], now: datetime
    ) -> None:
        kept = [(start, end) for start, end in intervals if end > now - USAGE_WINDOW]
        if not kept:
            await self.cache.delete(self._key("-usage", chat))
            return
        latest = max(end for _, end in kept)
        await self.cache.set(
            self._key("-usage", chat),
            json.dumps([[_iso(start), _iso(end)] for start, end in kept]).encode(),
            ttl_s=_ttl(latest + USAGE_WINDOW, now),
        )

    @staticmethod
    def _used(intervals: list[tuple[datetime, datetime]], at: datetime) -> timedelta:
        window_start = at - USAGE_WINDOW
        total = timedelta()
        for start, end in intervals:
            overlap = min(end, at) - max(start, window_start)
            if overlap > timedelta():
                total += overlap
        return total

    async def minutes_used(self, chat: ChatRef) -> float:
        now = self.clock()
        return self._used(await self._intervals(chat), now).total_seconds() / 60

    async def cooldown_until(self, chat: ChatRef) -> datetime | None:
        data = await self._read_json(self._key("-cooldown", chat))
        until = _parse_time(data) if isinstance(data, str) else None
        return until if until is not None and until > self.clock() else None

    async def switch(self, chat: ChatRef, model: str, *, set_by: str) -> SwitchResult:
        """Switch ``chat`` to ``model``, honouring the cooldown and the usage cap.

        Switching to the model that is already active doesn't extend it. The granted time is
        shortened so the chat stays within ``max_minutes`` per ``USAGE_WINDOW``."""
        async with self.cache.lock(self._key("-lock", chat), ttl_s=10):
            now = self.clock()
            current = await self.get(chat)
            if current is not None and current.model.casefold() == model.casefold():
                return SwitchResult(override=current, reason="already")
            cooldown = await self.cooldown_until(chat)
            if cooldown is not None:
                return SwitchResult(retry_at=cooldown, reason="cooldown")
            intervals = await self._intervals(chat)
            # A running override ends now if this switch goes ahead.
            closed = [(start, min(end, now)) for start, end in intervals]
            remaining = self.max_used - self._used(closed, now)
            granted = min(self.duration, remaining)
            if granted < timedelta(minutes=1):
                return SwitchResult(retry_at=self._cap_retry_at(closed, now), reason="cap")
            until = now + granted
            override = ModelOverride(model, set_by, until)
            await self.cache.set(
                self._key("", chat),
                json.dumps({"model": model, "set_by": set_by, "until": _iso(until)}).encode(),
                ttl_s=_ttl(until, now),
            )
            await self._save_intervals(chat, [*closed, (now, until)], now)
            if self.cooldown > timedelta():
                await self.cache.set(
                    self._key("-cooldown", chat),
                    json.dumps(_iso(now + self.cooldown)).encode(),
                    ttl_s=_ttl(now + self.cooldown, now),
                )
            return SwitchResult(override=override)

    def _cap_retry_at(self, intervals: list[tuple[datetime, datetime]], now: datetime) -> datetime:
        """The first whole minute at which at least one minute of override time is free."""
        limit = self.max_used - timedelta(minutes=1)
        for minutes in range(1, int(USAGE_WINDOW.total_seconds() // 60) + 1):
            at = now + timedelta(minutes=minutes)
            if self._used(intervals, at) <= limit:
                return at
        return now + USAGE_WINDOW

    async def clear(self, chat: ChatRef) -> ModelOverride | None:
        """End the override now. Doesn't touch the cooldown."""
        async with self.cache.lock(self._key("-lock", chat), ttl_s=10):
            now = self.clock()
            current = await self.get(chat)
            await self.cache.delete(self._key("", chat))
            if current is not None:
                intervals = await self._intervals(chat)
                await self._save_intervals(
                    chat, [(start, min(end, now)) for start, end in intervals], now
                )
            return current


@dataclass(frozen=True, slots=True)
class ModelSwitchConfig:
    """What an app passes to the agent and the ``/model`` command."""

    default_model: str
    choices: tuple[ModelChoice, ...]
    store: ModelOverrideStore
    anyone: bool = True
    """``LLM_MODEL_SWITCH=anyone``; ``False`` limits switching to admins and setup managers."""

    def choice(self, name: str) -> ModelChoice | None:
        return find_choice(self.choices, name)

    async def active(self, chat: ChatRef) -> tuple[ModelOverride, ModelChoice] | None:
        """The chat's override, if it is still on the configured list."""
        try:
            override = await self.store.get(chat)
        except Exception as exc:
            log.warning("model override read failed chat=%s error=%s", chat.key, type(exc).__name__)
            return None
        if override is None:
            return None
        choice = self.choice(override.model)
        if choice is None or choice.name.casefold() == self.default_model.casefold():
            return None
        return override, choice


def build_model_switch(
    *,
    provider_kind: str,
    default_model: str,
    choices_raw: str,
    cache: Cache,
    duration_minutes: int,
    max_minutes: int,
    cooldown_s: int,
    who: str,
    clock: Callable[[], datetime] | None = None,
) -> ModelSwitchConfig | None:
    """Validate the settings and build the config, or ``None`` when no choices are set."""
    choices = parse_model_choices(choices_raw)
    if not choices:
        return None
    if provider_kind != "openai_compat":
        raise ValueError("LLM_MODEL_CHOICES needs LLM_PROVIDER=openai_compat")
    if not default_model:
        raise ValueError("LLM_MODEL_CHOICES needs LLM_MODEL")
    if who not in {"anyone", "admins"}:
        raise ValueError("LLM_MODEL_SWITCH must be anyone or admins")
    store = ModelOverrideStore(
        cache,
        duration_minutes=duration_minutes,
        max_minutes=max_minutes,
        cooldown_s=cooldown_s,
        clock=clock,
    )
    return ModelSwitchConfig(default_model, choices, store, anyone=who == "anyone")
