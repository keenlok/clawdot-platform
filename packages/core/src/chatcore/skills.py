"""Trusted declarations and fresh per-request availability checks for skills."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from chatcore.events import ChatRef
from chatcore.tools.registry import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from chatcore.plugin import Plugin

log = logging.getLogger(__name__)


class GateState(StrEnum):
    AVAILABLE = "available"
    OFF = "off"  # A toggle is off in this scope; someone can enable it.
    NOT_ALLOWED = "not_allowed"  # This person cannot use it here.
    UNCONFIGURED = "unconfigured"  # It is not in this deployment; never offered or mentioned.


@dataclass(frozen=True, slots=True)
class Gate:
    state: GateState
    hint: str = ""


@dataclass(frozen=True, slots=True)
class ToolGate:
    scope: Callable[[ChatRef, bool], Awaitable[Gate]] | None = None
    """Deployment config and per-chat toggles only, keyed as the plugin stores them
    ((chat_key, is_dm) today). Must not read the user or tool arguments. Not cached."""
    user: Callable[[ToolContext], Awaitable[Gate]] | None = None
    """Admin/moderator/setup-manager/membership/linked-group checks. Not cached."""
    variant_hints: Callable[[ChatRef, bool], Awaitable[tuple[str, ...]]] | None = None
    """Hints for restricted variants of an offered tool, e.g. rendered watches are off here
    while plain watches are allowed. The handler still refuses the variant."""


@dataclass(frozen=True, slots=True)
class Skill:
    name: str  # ^[a-z][a-z0-9_]{0,47}$, unique.
    summary: str  # At most 120 characters; shown in the use_skill catalog.
    examples: tuple[
        str, ...
    ]  # Four to twelve trigger phrasings in English, Chinese and Singlish; routing only.
    tools: tuple[str, ...]  # Tools exposed when active (the required set).
    optional_tools: tuple[
        str, ...
    ] = ()  # Composite-only tools, offered when available and otherwise hinted.
    instructions: str = ""  # At most 800 characters; trusted and injected only when active.
    keywords: tuple[str, ...] = ()  # Casefolded literal hints, such as ``remind`` and ``提醒``.
    commands: tuple[str, ...] = ()  # Related slash commands for help and hints.
    max_steps: int | None = None  # Clamped to the global agent tool-step cap; never raises it.
    # Raises the turn's per-step output limit while active, clamped to AgentSettings ceiling.
    max_output_tokens: int | None = None
    sticky_turns: int = 2  # How many later requester turns may inherit this skill.
    context: Callable[[ToolContext], Awaitable[str | None]] | None = None
    """Trusted per-turn text appended to ``instructions`` while active. Build it only from
    validated, enumerated values. Text over 400 characters is dropped and logged; errors are
    logged by class name and skipped."""
    pin: Callable[[ToolContext], Awaitable[bool]] | None = None
    """Return true to make the skill active this turn without a router hit.
    Errors count as false."""


# Offered on every routed turn unless an app passes its own list (``()`` opts out).
DEFAULT_GENERAL_TOOLS: tuple[str, ...] = (
    "read_explainer",
    "get_current_time",
    "read_chat_history",
    "read_attachment",
    "recall",
    "search_web",
    "fetch_web_page",
)


class SkillsMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


@dataclass(frozen=True, slots=True)
class SkillAvailability:
    state: GateState
    offered: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()
    variant_hints: tuple[str, ...] = ()


def render_hint(hint: str, command_form: Callable[[str], str]) -> str:
    return re.sub(r"\{cmd:([a-z][a-z0-9_]*)\}", lambda match: command_form(match.group(1)), hint)


def _name_hash(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]


_SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_IDENTIFIER = re.compile(r"[^a-z0-9_]")
_STATE_RANK = {
    GateState.AVAILABLE: 0,
    GateState.OFF: 1,
    GateState.NOT_ALLOWED: 2,
    GateState.UNCONFIGURED: 3,
}


class SkillSet:
    def __init__(
        self,
        registry: ToolRegistry,
        skills: dict[str, tuple[Skill, str]],
        owners: dict[str, str],
        gates: dict[str, ToolGate],
        general: tuple[str, ...],
        generated: tuple[str, ...],
    ) -> None:
        self.registry = registry
        self._skills = skills
        self._owners = owners
        self._gates = gates
        self.general: tuple[str, ...] = general
        self.generated: tuple[str, ...] = generated

    @classmethod
    def build(
        cls,
        plugins: Sequence[Plugin],
        registry: ToolRegistry,
        *,
        composites: Sequence[Skill] = (),
        general_tools: Sequence[str] = (),
        step_cap: int = 8,
        instructions: Mapping[str, str] | None = None,
        instruction_additions: Mapping[str, str] | None = None,
    ) -> SkillSet:
        """Build the catalog.

        Each skill's own ``instructions`` (declared by its plugin) are the default.
        ``instructions`` replaces them for the named skills; ``instruction_additions`` appends
        bot-specific text after them. The 800-character cap applies to the result.
        """
        owners = {}
        plugin_tools = {}
        gates = {}
        declared = []
        seen = {}
        for plugin in plugins:
            tools = plugin.tools()
            plugin_tools[plugin.name] = {tool.name for tool in tools}
            for tool in tools:
                if registry.get(tool.name) is not None:
                    owners[tool.name] = plugin.name
            gate_hook = getattr(plugin, "tool_gates", None)
            plugin_gates = gate_hook() if callable(gate_hook) else {}
            for tool_name, gate in plugin_gates.items():
                if tool_name not in plugin_tools[plugin.name]:
                    raise ValueError(f"plugin {plugin.name} supplies a gate for tool {tool_name}")
                gates[tool_name] = gate
            hook = getattr(plugin, "skills", None)
            if callable(hook):
                for skill in hook():
                    cls._validate(skill, plugin.name, composite=False)
                    if skill.name in seen:
                        raise ValueError(
                            f"duplicate skill {skill.name} from {plugin.name} and "
                            f"{seen[skill.name]}"
                        )
                    seen[skill.name] = plugin.name
                    declared.append((skill, plugin.name, False))
        for skill in composites:
            cls._validate(skill, "composite", composite=True)
            if skill.name in seen:
                raise ValueError(
                    f"duplicate skill {skill.name} from composite and {seen[skill.name]}"
                )
            seen[skill.name] = "composite"
            declared.append((skill, "composite", True))

        loaded = {}
        for skill, owner, composite in declared:
            present = tuple(name for name in skill.tools if registry.get(name) is not None)
            missing = set(skill.tools) - set(present)
            if composite and missing:
                log.warning("skill not loaded skill=%s reason=missing_required", skill.name)
                continue
            if not composite:
                for name in skill.tools:
                    if name in missing:
                        log.warning("skill tool missing skill=%s tool=%s", skill.name, name)
            optional = tuple(
                name for name in skill.optional_tools if registry.get(name) is not None
            )
            if not composite:
                optional = ()
            elif len(optional) != len(skill.optional_tools):
                log.debug("skill optional tool missing skill=%s", skill.name)
            if not present and not optional:
                log.warning("skill not loaded skill=%s reason=empty", skill.name)
                continue
            loaded[skill.name] = (cls._clamp(skill, step_cap, present, optional), owner)

        for name, instructions_text in (instructions or {}).items():
            if name not in loaded:
                log.warning("skill instructions ignored skill=%s", name)
                continue
            if len(instructions_text) > 800:
                raise ValueError(f"skill {name}: instructions exceed 800 characters")
            skill, owner = loaded[name]
            loaded[name] = (replace(skill, instructions=instructions_text), owner)
        for name, addition in (instruction_additions or {}).items():
            if name not in loaded:
                log.warning("skill instruction addition ignored skill=%s", name)
                continue
            skill, owner = loaded[name]
            merged = f"{skill.instructions} {addition}".strip()
            if len(merged) > 800:
                raise ValueError(f"skill {name}: instructions exceed 800 characters")
            loaded[name] = (replace(skill, instructions=merged), owner)

        general = tuple(dict.fromkeys(name for name in general_tools if registry.get(name)))
        covered = set(general)
        for skill, _owner in loaded.values():
            covered.update(skill.tools)
            covered.update(skill.optional_tools)
        by_plugin = {}
        for name in owners:
            if name not in covered:
                by_plugin.setdefault(owners[name], []).append(name)
        generated = []
        generated_owners = {}
        for plugin_name, names in by_plugin.items():
            normalized = _IDENTIFIER.sub("_", plugin_name.casefold())
            if not normalized or not normalized[0].isalpha():
                normalized = "p_" + normalized
            generated_name = normalized[:32] + "_" + _name_hash(plugin_name) + "_other"
            previous = seen.get(generated_name) or generated_owners.get(generated_name)
            if previous:
                raise ValueError(f"generated skill collision between {previous} and {plugin_name}")
            descriptions = tuple(registry.get(name).description[:200] for name in names)
            generated_skill = Skill(
                generated_name,
                f"Other {plugin_name} tools"[:120],
                descriptions,
                tuple(names),
            )
            loaded[generated_name] = (generated_skill, plugin_name)
            generated.append(generated_name)
            generated_owners[generated_name] = plugin_name
            log.warning(
                "skill generated plugin=%s skill=%s tools=%d",
                plugin_name,
                generated_name,
                len(names),
            )
        return cls(registry, loaded, owners, gates, general, tuple(generated))

    @staticmethod
    def _validate(skill, owner, *, composite):
        def invalid(reason):
            raise ValueError(f"skill {skill.name!r} from {owner}: {reason}")

        if not _SKILL_NAME.fullmatch(skill.name):
            invalid("invalid name")
        if len(skill.summary) > 120:
            invalid("summary exceeds 120 characters")
        if len(skill.instructions) > 800:
            invalid("instructions exceed 800 characters")
        if not 4 <= len(skill.examples) <= 12:
            invalid("examples must contain 4 to 12 items")
        if any(word != word.casefold() for word in skill.keywords):
            invalid("keywords must be casefolded")
        if skill.sticky_turns < 1:
            invalid("sticky_turns must be at least 1")
        if skill.max_output_tokens is not None and skill.max_output_tokens < 1:
            invalid("max_output_tokens must be at least 1")
        if skill.context is not None and not callable(skill.context):
            invalid("context must be callable")
        if skill.pin is not None and not callable(skill.pin):
            invalid("pin must be callable")
        if skill.optional_tools and not composite:
            invalid("optional_tools are only allowed on composites")

    @staticmethod
    def _clamp(skill, cap, tools, optional):
        steps = None if skill.max_steps is None else max(1, min(cap, skill.max_steps))
        return replace(skill, tools=tools, optional_tools=optional, max_steps=steps)

    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def get(self, name: str) -> Skill | None:
        entry = self._skills.get(name)
        return entry[0] if entry else None

    def owner(self, tool: str) -> str | None:
        return self._owners.get(tool)

    def gate(self, tool: str) -> ToolGate | None:
        return self._gates.get(tool)

    async def evaluate_tool(self, name: str, ctx: ToolContext) -> Gate:
        tool = self.registry.get(name)
        if tool is None:
            return Gate(GateState.UNCONFIGURED)
        if tool.admin_only and not ctx.is_admin:
            return Gate(GateState.NOT_ALLOWED)
        gate = self._gates.get(name)
        if gate is None:
            return Gate(GateState.AVAILABLE)
        results = []
        try:
            if gate.scope:
                results.append(await gate.scope(ctx.chat, ctx.is_dm))
            if gate.user:
                results.append(await gate.user(ctx))
        except Exception as exc:
            log.warning("tool gate failed tool=%s error=%s", name, type(exc).__name__)
            return Gate(GateState.NOT_ALLOWED)
        if not results:
            return Gate(GateState.AVAILABLE)
        return max(results, key=lambda result: _STATE_RANK[result.state])

    async def evaluate_skill(self, skill_name: str, ctx: ToolContext) -> SkillAvailability:
        skill = self.get(skill_name)
        if skill is None:
            return SkillAvailability(GateState.UNCONFIGURED)
        required = [(name, await self.evaluate_tool(name, ctx)) for name in skill.tools]
        optional = [(name, await self.evaluate_tool(name, ctx)) for name in skill.optional_tools]
        available_required = [name for name, gate in required if gate.state is GateState.AVAILABLE]
        offered = available_required + [
            name for name, gate in optional if gate.state is GateState.AVAILABLE
        ]
        if available_required:
            state = GateState.AVAILABLE
        elif not required:
            state = GateState.UNCONFIGURED
        else:
            state = min((gate.state for _, gate in required), key=lambda item: _STATE_RANK[item])
        hints = tuple(
            dict.fromkeys(
                gate.hint
                for _, gate in [*required, *optional]
                if gate.state in {GateState.OFF, GateState.NOT_ALLOWED} and gate.hint
            )
        )
        variants = []
        for name in offered:
            gate = self._gates.get(name)
            if gate and gate.variant_hints:
                try:
                    variants.extend(await gate.variant_hints(ctx.chat, ctx.is_dm))
                except Exception as exc:
                    log.warning("tool gate failed tool=%s error=%s", name, type(exc).__name__)
        allowed = {tool.name for tool in self.registry.available(is_admin=ctx.is_admin)}
        offered = list(dict.fromkeys(name for name in offered if name in allowed))
        return SkillAvailability(state, tuple(offered), hints, tuple(dict.fromkeys(variants)))
