"""Per-turn routing, tool availability and continuation state."""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import Field
from pydantic_settings import BaseSettings

from chatcore.llm.types import ToolSpec
from chatcore.skills import Gate, GateState, SkillAvailability, SkillsMode, render_hint
from chatcore.threads import current_conversation
from chatcore.tools.registry import ToolContext, ToolResult

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SkillSettings:
    mode: SkillsMode = SkillsMode.OFF
    chats: frozenset[str] = frozenset()
    other_chats: SkillsMode = SkillsMode.OFF
    max_active: int = 3
    max_loads: int = 2
    max_meta_calls: int = 3
    sticky_turns: int = 2
    router: str = "embed"
    router_timeout_ms: int = 300
    embed_threshold: float = 0.5
    embed_margin: float = 0.05
    tool_steps: int = 8


class SkillsEnvSettings(BaseSettings):
    """Skill-layer router settings read from ``SKILLS_*`` environment variables.

    Shared by every app so each bot gets the same names, defaults and validation. Apps load it
    with their own settings loader and call ``validate()`` then ``to_settings()``.
    """

    model_config = {"env_prefix": "", "env_file": None, "extra": "ignore"}

    SKILLS_MODE: str = "off"
    SKILLS_CHATS: str = ""
    SKILLS_OTHER_CHATS: str = "off"
    SKILLS_MAX_ACTIVE: int = Field(default=3, ge=1)
    SKILLS_MAX_LOADS: int = Field(default=2, ge=1)
    SKILLS_MAX_META_CALLS: int = Field(default=3, ge=1)
    SKILLS_STICKY_TURNS: int = Field(default=2, ge=1)
    SKILLS_ROUTER: str = "embed"
    SKILLS_ROUTER_TIMEOUT_MS: int = Field(default=300, ge=50, le=5000)
    SKILLS_EMBED_THRESHOLD: float = Field(default=0.5, ge=0, le=1)
    SKILLS_EMBED_MARGIN: float = Field(default=0.05, ge=0, le=0.5)

    def validate_modes(self) -> None:
        if self.SKILLS_MODE not in {"off", "shadow", "on"}:
            raise ValueError("SKILLS_MODE must be off, shadow or on")
        if self.SKILLS_OTHER_CHATS not in {"off", "shadow"}:
            raise ValueError("SKILLS_OTHER_CHATS must be off or shadow")
        if self.SKILLS_ROUTER not in {"embed", "keyword"}:
            raise ValueError("SKILLS_ROUTER must be embed or keyword")
        if self.SKILLS_MODE == "on" and not self.SKILLS_CHATS.strip():
            log.warning("skills on but SKILLS_CHATS empty")

    def to_settings(self, *, tool_steps: int) -> SkillSettings:
        return SkillSettings(
            mode=SkillsMode(self.SKILLS_MODE),
            chats=frozenset(item.strip() for item in self.SKILLS_CHATS.split(",") if item.strip()),
            other_chats=SkillsMode(self.SKILLS_OTHER_CHATS),
            max_active=self.SKILLS_MAX_ACTIVE,
            max_loads=self.SKILLS_MAX_LOADS,
            max_meta_calls=self.SKILLS_MAX_META_CALLS,
            sticky_turns=self.SKILLS_STICKY_TURNS,
            router=self.SKILLS_ROUTER,
            router_timeout_ms=self.SKILLS_ROUTER_TIMEOUT_MS,
            embed_threshold=self.SKILLS_EMBED_THRESHOLD,
            embed_margin=self.SKILLS_EMBED_MARGIN,
            tool_steps=tool_steps,
        )


def effective_mode(settings: SkillSettings, chat) -> SkillsMode:
    if settings.mode is SkillsMode.OFF:
        return SkillsMode.OFF
    if settings.mode is SkillsMode.SHADOW:
        return SkillsMode.SHADOW
    if "*" in settings.chats or chat.key in settings.chats:
        return SkillsMode.ON
    return settings.other_chats


async def _availability_from_snapshot(
    app, name: str, ctx: ToolContext, gates: dict
) -> SkillAvailability:
    skill = app.skills.get(name)
    if skill is None:
        return SkillAvailability(GateState.UNCONFIGURED)
    required = [(tool, gates.get(tool, Gate(GateState.UNCONFIGURED))) for tool in skill.tools]
    optional = [
        (tool, gates.get(tool, Gate(GateState.UNCONFIGURED))) for tool in skill.optional_tools
    ]
    available_required = [tool for tool, gate in required if gate.state is GateState.AVAILABLE]
    offered = available_required + [
        tool for tool, gate in optional if gate.state is GateState.AVAILABLE
    ]
    if available_required:
        state = GateState.AVAILABLE
    elif not required:
        state = GateState.UNCONFIGURED
    else:
        rank = {
            GateState.AVAILABLE: 0,
            GateState.OFF: 1,
            GateState.NOT_ALLOWED: 2,
            GateState.UNCONFIGURED: 3,
        }
        state = min((gate.state for _, gate in required), key=lambda value: rank[value])
    hints = tuple(
        dict.fromkeys(
            gate.hint
            for _, gate in [*required, *optional]
            if gate.state in {GateState.OFF, GateState.NOT_ALLOWED} and gate.hint
        )
    )
    variants = []
    for tool_name in offered:
        gate = app.skills.gate(tool_name)
        if gate and gate.variant_hints:
            try:
                variants.extend(await gate.variant_hints(ctx.chat, ctx.is_dm))
            except Exception as exc:
                log.warning("tool gate failed tool=%s error=%s", tool_name, type(exc).__name__)
    available = {tool.name for tool in app.tools.available(is_admin=ctx.is_admin, ctx=ctx)}
    offered = list(dict.fromkeys(tool for tool in offered if tool in available))
    return SkillAvailability(state, tuple(offered), hints, tuple(dict.fromkeys(variants)))


@dataclass(slots=True)
class SkillTurn:
    mode: SkillsMode
    active: list[str]
    loaded: list[str]
    offered: dict[str, ToolSpec]
    hints: list[tuple[str, str]]
    variant_hints: list[str]
    step_cap: int
    meta_calls: int = 0
    route_result: object | None = None
    availability: dict[str, SkillAvailability] = field(default_factory=dict)
    all_availability: dict[str, SkillAvailability] = field(default_factory=dict)
    initial_offered: frozenset[str] = frozenset()
    eligible_snapshot: dict[str, GateState] = field(default_factory=dict)
    gate_snapshot: dict[str, Gate] = field(default_factory=dict)
    gate_ms: float = 0
    requester: str = ""
    settings: SkillSettings = field(default_factory=SkillSettings)
    _app: object | None = None
    _ctx: ToolContext | None = None
    _state: dict[str, tuple[int, datetime]] = field(default_factory=dict)
    _fresh: set[str] = field(default_factory=set)
    _refreshed: set[str] = field(default_factory=set)
    _inherited_crowded: set[str] = field(default_factory=set)
    _called_outside_capping_skill: bool = False

    @classmethod
    async def build(
        cls,
        *,
        app,
        ctx: ToolContext,
        message,
        text: str,
        settings: SkillSettings,
        router,
        store,
        now: datetime,
    ) -> SkillTurn:
        mode = effective_mode(settings, message.chat)
        route = await router.route(text) if router is not None else None
        hits = [hit.skill for hit in route.hits] if route else []
        pinned = []
        if mode is not SkillsMode.OFF:
            for name in app.skills.names():
                skill = app.skills.get(name)
                if skill is None or skill.pin is None:
                    continue
                try:
                    if await skill.pin(ctx):
                        pinned.append(name)
                except Exception as exc:
                    log.warning("skill pin failed skill=%s error=%s", name, type(exc).__name__)
        conversation = current_conversation.get()
        state = await store.latest_skill_state(
            message.chat,
            thread_id=message.thread_id,
            conversation_id=conversation.id if conversation else None,
            requester=message.user.ref.key,
            since=now - timedelta(minutes=30),
            repo=app.stores.repo,
        )
        inherited = []
        inherited_state = {}
        if state:
            for item in state.get("skills", []):
                name = item["name"]
                if app.skills.get(name) is None or item["left"] < 1:
                    continue
                try:
                    fresh_at = datetime.fromisoformat(item["fresh_at"])
                except ValueError:
                    continue
                if fresh_at.tzinfo is None or fresh_at < now - timedelta(minutes=30):
                    continue
                inherited_state[name] = (item["left"], fresh_at.astimezone(UTC))
                if name not in hits:
                    inherited.append(name)
        ordered = list(
            dict.fromkeys(
                pinned + hits + sorted(inherited, key=lambda n: inherited_state[n][1], reverse=True)
            )
        )
        gate_snapshot = {}
        gate_ms = 0
        all_availability = {}
        if mode is SkillsMode.SHADOW:
            gate_started = time.perf_counter()
            tools = app.tools.available(is_admin=True, ctx=ctx)
            by_gate = {}
            for tool in tools:
                gate = app.skills.gate(tool.name)
                if gate is not None:
                    by_gate.setdefault(id(gate), (gate, []))[1].append(tool)
            cached = {}
            for gate_id, (_gate, owned_tools) in by_gate.items():
                representative = next(
                    (tool for tool in owned_tools if not tool.admin_only), owned_tools[0]
                )
                cached[gate_id] = await app.skills.evaluate_tool(representative.name, ctx)
            for tool in tools:
                gate = app.skills.gate(tool.name)
                result = cached.get(id(gate)) if gate is not None else Gate(GateState.AVAILABLE)
                if tool.admin_only and not ctx.is_admin:
                    result = Gate(GateState.NOT_ALLOWED)
                gate_snapshot[tool.name] = result
            all_availability = {
                name: await _availability_from_snapshot(app, name, ctx, gate_snapshot)
                for name in app.skills.names()
            }
            gate_ms = (time.perf_counter() - gate_started) * 1000
        availability = {}
        candidates = []
        for name in ordered:
            available = (
                all_availability[name]
                if mode is SkillsMode.SHADOW
                else await app.skills.evaluate_skill(name, ctx)
            )
            if available.state is GateState.UNCONFIGURED:
                inherited_state.pop(name, None)
                continue
            availability[name] = available
            if available.offered or (
                available.state in {GateState.OFF, GateState.NOT_ALLOWED} and available.hints
            ):
                candidates.append(name)
        active = candidates[: settings.max_active]
        crowded = set(candidates[settings.max_active :])
        turn = cls(
            mode=mode,
            active=active,
            loaded=[],
            offered={},
            hints=[],
            variant_hints=[],
            step_cap=settings.tool_steps,
            route_result=route,
            availability={name: availability[name] for name in active},
            all_availability=availability,
            requester=message.user.ref.key,
            settings=settings,
            _app=app,
            _ctx=ctx,
            _state=inherited_state,
            _fresh=set(hits) | set(pinned),
            _inherited_crowded=crowded,
            gate_snapshot=gate_snapshot,
            eligible_snapshot={name: gate.state for name, gate in gate_snapshot.items()},
            gate_ms=gate_ms,
        )
        turn.all_availability = all_availability or availability
        if mode is SkillsMode.ON:
            for name in app.skills.names():
                if name not in turn.all_availability:
                    turn.all_availability[name] = await app.skills.evaluate_skill(name, ctx)
        general = []
        for name in app.skills.general:
            if app.tools.get(name) is None:
                continue
            gate = (
                gate_snapshot.get(name, Gate(GateState.UNCONFIGURED))
                if mode is SkillsMode.SHADOW
                else await app.skills.evaluate_tool(name, ctx)
            )
            if gate.state is GateState.AVAILABLE:
                general.append(name)
            elif gate.state in {GateState.OFF, GateState.NOT_ALLOWED} and gate.hint:
                turn._add_hint("__general__", gate.hint)
        turn._add_skill_availability(active)
        offered_names = list(
            dict.fromkeys(
                general + [tool for name in active for tool in availability[name].offered]
            )
        )
        if mode is SkillsMode.ON:
            turn.offered.update({name: app.tools.get(name).spec for name in offered_names})
            turn._refresh_use_skill_spec()
            for name, _spec in turn.offered.items():
                if name != "use_skill":
                    assert app.tools.get(name) is not None
                    assert name in {
                        tool.name for tool in app.tools.available(is_admin=ctx.is_admin, ctx=ctx)
                    }
        turn.initial_offered = frozenset(offered_names)
        turn.step_cap = turn._current_step_cap()
        return turn

    def _add_hint(self, skill: str, hint: str) -> None:
        assert self._app is not None and self._ctx is not None
        rendered = self._render_hint(hint)
        item = (skill, rendered)
        if item not in self.hints:
            self.hints.append(item)

    def _render_hint(self, hint: str) -> str:
        assert self._app is not None and self._ctx is not None
        return render_hint(
            hint, lambda command: self._app.command_form(command, is_dm=self._ctx.is_dm)
        )

    def _add_skill_availability(self, names: Sequence[str]) -> None:
        for name in names:
            available = self.availability.get(name)
            if not available:
                continue
            for hint in available.hints:
                self._add_hint(name, hint)
            for hint in available.variant_hints:
                rendered = self._render_hint(hint)
                if rendered not in self.variant_hints:
                    self.variant_hints.append(rendered)

    def offered_snapshot(self) -> frozenset[str]:
        return frozenset(self.offered)

    def check_call(self, name: str, snapshot: frozenset[str]) -> str:
        if name == "use_skill":
            return "meta"
        if name in snapshot:
            return "ok"
        return "not_offered" if self._app.tools.get(name) else "unknown"

    async def use_skill(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        if set(arguments) != {"name"} or not isinstance(arguments.get("name"), str):
            return _meta_error("use_skill needs one argument: name.")
        name = arguments["name"]
        skill = self._app.skills.get(name)
        if skill is None:
            return _meta_error("Unknown skill.")
        if name in self.active or name in self.loaded:
            return _meta_error("That skill is already loaded.")
        if len(self.loaded) >= self.settings.max_loads:
            return _meta_error("Skill load limit reached for this turn.")
        available = await self._app.skills.evaluate_skill(name, ctx)
        if available.state is GateState.UNCONFIGURED:
            return _meta_error("Unknown skill.")
        if available.state in {GateState.OFF, GateState.NOT_ALLOWED}:
            for hint in available.hints:
                self._add_hint(name, hint)
            return ToolResult(
                "\n".join(
                    render_hint(
                        hint, lambda command: self._app.command_form(command, is_dm=ctx.is_dm)
                    )
                    for hint in available.hints
                )
                or "That skill is unavailable here.",
                is_error=True,
                persist_content=False,
            )
        self.loaded.append(name)
        self.availability[name] = available
        self.all_availability[name] = available
        self._refreshed.add(name)
        for hint in available.hints:
            self._add_hint(name, hint)
        self.variant_hints.extend(
            rendered
            for rendered in (self._render_hint(hint) for hint in available.variant_hints)
            if rendered not in self.variant_hints
        )
        self.offered.pop("use_skill", None)
        for tool_name in available.offered:
            tool = self._app.tools.get(tool_name)
            if tool and (ctx.is_admin or not tool.admin_only):
                self.offered[tool_name] = tool.spec
        self._refresh_use_skill_spec()
        self.step_cap = max(self.step_cap, self._current_step_cap())
        tool_list = ", ".join(available.offered) or "no tools"
        content = f"Loaded {name}: tools {tool_list} are available from your next step."
        if skill.instructions:
            content += " " + skill.instructions
        rendered_hints = [self._render_hint(hint) for hint in available.hints]
        if rendered_hints:
            content += " Unavailable tools: " + " ".join(rendered_hints)
        rendered_variants = [self._render_hint(hint) for hint in available.variant_hints]
        if rendered_variants:
            content += " " + " ".join(rendered_variants)
        return ToolResult(content, persist_content=False)

    def _loadable(self) -> list[str]:
        names = []
        for name in self._app.skills.names():
            if name in self.active or name in self.loaded:
                continue
            available = self.all_availability.get(name)
            if available is not None and available.state is not GateState.UNCONFIGURED:
                names.append(name)
        return names

    def _refresh_use_skill_spec(self) -> None:
        if self.mode is not SkillsMode.ON or not self._app.skills.names():
            return
        names = self._loadable()
        if not names or len(self.loaded) >= self.settings.max_loads:
            self.offered.pop("use_skill", None)
            return
        lines = []
        for name in names:
            summary = " ".join(self._app.skills.get(name).summary.split())
            line = f"{name} — {summary}"
            if sum(len(existing) + 1 for existing in lines) + len(line) > 1000:
                break
            lines.append(line)
        if not lines:
            return
        self.offered["use_skill"] = ToolSpec(
            "use_skill",
            "\n".join(lines),
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name",
                        "enum": [line.split(" — ", 1)[0] for line in lines],
                    }
                },
                "required": ["name"],
            },
        )

    def _capping_skills(self) -> dict[str, tuple[int, tuple[str, ...]]]:
        names = [*self.active, *self.loaded]
        return {
            name: (skill.max_steps, available.offered)
            for name in names
            if (skill := self._app.skills.get(name)) is not None
            and skill.max_steps is not None
            and (available := self.availability.get(name)) is not None
            and available.offered
        }

    def output_tokens(self, default: int, ceiling: int) -> int:
        names = [*self.active, *self.loaded]
        requested = [default]
        for name in names:
            skill = self._app.skills.get(name) if self._app is not None else None
            available = self.availability.get(name)
            if (
                skill is not None
                and skill.max_output_tokens is not None
                and available is not None
                and available.offered
            ):
                requested.append(skill.max_output_tokens)
        return min(max(requested), max(default, ceiling))

    def _current_step_cap(self) -> int:
        capping_skills = self._capping_skills()
        if self._called_outside_capping_skill:
            return self.settings.tool_steps
        return max(
            (max_steps for max_steps, _offered in capping_skills.values()),
            default=self.settings.tool_steps,
        )

    def note_call(self, tool_name: str) -> None:
        capping_tools = {
            offered_tool
            for _max_steps, offered_tools in self._capping_skills().values()
            for offered_tool in offered_tools
        }
        if tool_name not in capping_tools:
            self._called_outside_capping_skill = True
        self.step_cap = max(self.step_cap, self._current_step_cap())

    def note_executed(self, tool_name: str) -> None:
        for name in [*self.active, *self.loaded, *self._inherited_crowded]:
            skill = self._app.skills.get(name)
            if skill and (tool_name in skill.tools or tool_name in skill.optional_tools):
                self._refreshed.add(name)

    def continuation_record(self, now: datetime) -> dict:
        state = dict(self._state)
        considered = set(self.active) | set(self.loaded) | self._inherited_crowded
        for name in considered:
            skill = self._app.skills.get(name)
            if skill is None:
                continue
            if name in self._fresh or name in self._refreshed:
                left = min(skill.sticky_turns, self.settings.sticky_turns)
                fresh_at = now.astimezone(UTC)
            else:
                old_left, fresh_at = state.get(name, (1, now.astimezone(UTC)))
                left = old_left - 1
            if left > 0:
                state[name] = (left, fresh_at)
            else:
                state.pop(name, None)
        entries = sorted(state.items(), key=lambda item: item[1][1], reverse=True)
        entries = entries[: self.settings.max_active + self.settings.max_loads]
        return {
            "requester": self.requester,
            "skills": [
                {"name": name, "left": left, "fresh_at": fresh_at.isoformat()}
                for name, (left, fresh_at) in entries
                if left > 0
            ],
        }

    def metric_fields(self, called: Sequence[str]) -> dict:
        route = self.route_result
        first = self.active[0] if self.active else None
        source = "none"
        if first:
            if first in self._fresh:
                hit = next((h for h in route.hits if h.skill == first), None) if route else None
                source = hit.source if hit else "keyword"
            else:
                source = "sticky"
        fields = {
            "skills": ",".join(self.active) or "-",
            "loaded": ",".join(self.loaded) or "-",
            "source": source,
            "embed_skipped": getattr(route, "embed_skipped", "disabled"),
            "top": ",".join(f"{name}:{score:.2f}" for name, score in getattr(route, "top", ()))
            or "-",
            "meta_calls": self.meta_calls,
            "hints": ",".join(dict.fromkeys(name for name, _ in self.hints)) or "-",
            "route_ms": int(getattr(route, "route_ms", 0)),
            "eligible_calls": "-",
            "unevaluated": "-",
            "initial_cover": "-",
            "oracle_cover": "-",
            "unavailable_routed": "-",
            "gate_ms": "-",
        }
        if self.mode is SkillsMode.SHADOW:
            valid_called = [name for name in called if self._app.tools.get(name) is not None]
            unevaluated = sum(name not in self.eligible_snapshot for name in valid_called)
            eligible = [
                name
                for name in valid_called
                if self.eligible_snapshot.get(name) is GateState.AVAILABLE
            ]
            fields["eligible_calls"] = len(eligible)
            fields["unevaluated"] = unevaluated
            fields["gate_ms"] = int(self.gate_ms)
            if eligible and not unevaluated:
                initial = sum(name in self.initial_offered for name in eligible)
                fields["initial_cover"] = f"{initial}/{len(eligible)}"
                skill_names = [
                    name
                    for name, item in self.all_availability.items()
                    if item.state is GateState.AVAILABLE
                ]
                oracle = sum(name in self.initial_offered for name in eligible)
                for count in range(1, min(self.settings.max_loads, len(skill_names)) + 1):
                    cover = set(self.initial_offered)
                    for selected in itertools.combinations(skill_names, count):
                        cover.update(
                            tool
                            for name in selected
                            for tool in self.all_availability[name].offered
                        )
                        oracle = max(oracle, sum(name in cover for name in eligible))
                        cover = set(self.initial_offered)
                fields["oracle_cover"] = f"{oracle}/{len(eligible)}"
            routed_names = set(self.active) | set(self._fresh)
            unavailable = set()
            for name in routed_names:
                item = self.all_availability.get(name)
                if item and item.hints:
                    unavailable.update(
                        tool
                        for tool in self._app.skills.get(name).tools
                        if self.eligible_snapshot.get(tool)
                        in {GateState.OFF, GateState.NOT_ALLOWED}
                    )
            routed_tools = set()
            for name in routed_names:
                skill = self._app.skills.get(name)
                if skill:
                    routed_tools.update(skill.tools)
            fields["unavailable_routed"] = int(
                bool(unavailable & (set(valid_called) | routed_tools))
            )
        return fields


def _meta_error(text: str) -> ToolResult:
    return ToolResult(text, is_error=True, persist_content=False)
