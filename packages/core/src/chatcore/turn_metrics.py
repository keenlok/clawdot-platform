"""Stage-0 counters for one agent turn."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from chatcore.llm.types import ToolSpec, Usage
from chatcore.tools.registry import ToolRegistry


class TurnMetrics:
    """Per-turn counters for the stage-0 baseline log line.

    Holds only registry-validated tool names, categories and counts. Never stores message text,
    tool arguments, URLs, model output or unvalidated names.
    """

    _FINAL_FORCED = {"deadline", "budget", "steps", "rounds", "meta", "-"}
    _ENDS = {"answer", "pending", "refusal", "timeout", "provider_error", "fallback"}

    def __init__(self, registry: ToolRegistry, offered: Sequence[ToolSpec]) -> None:
        self._registry = registry
        self._offered_names = {spec.name for spec in offered}
        self.offered = len(offered)
        payload = [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
            for spec in offered
        ]
        self.spec_bytes = len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        self.called: list[str] = []
        self.unknown_calls = 0
        self.not_offered = 0
        self.markup_stripped = 0
        self.retries = 0
        self.skill_fields: dict[str, object] = {}
        self.mode = "off"

    def note_call(self, name: str, *, offered: bool | None = None) -> None:
        if self._registry.get(name) is None:
            self.unknown_calls += 1
            return
        if offered is False or (offered is None and name not in self._offered_names):
            self.not_offered += 1
        if name not in self.called:
            self.called.append(name)

    def note_enforcement(self, name: str, *, unknown: bool) -> None:
        if unknown:
            self.unknown_calls += 1
            return
        self.not_offered += 1
        if name not in self.called:
            self.called.append(name)

    def note_stripped(self) -> None:
        self.markup_stripped += 1

    def note_retry(self) -> None:
        self.retries += 1

    def emit(
        self,
        logger: logging.Logger,
        *,
        chat_key: str,
        steps: int,
        final_forced: str,
        end: str,
        usage: Usage,
        ms: float,
        mode: str = "off",
        skill_fields: dict[str, object] | None = None,
        model: str = "-",
        vision: bool | None = None,
    ) -> None:
        if final_forced not in self._FINAL_FORCED:
            raise ValueError("invalid final_forced category")
        if end not in self._ENDS:
            raise ValueError("invalid end category")
        called = ",".join(self.called) or "-"
        logger.info(
            "turn metrics chat=%s mode=%s offered=%d spec_bytes=%d steps=%d called=%s "
            "unknown_calls=%d not_offered=%d final_forced=%s markup_stripped=%d retries=%d "
            "end=%s in_tokens=%d out_tokens=%d ms=%d "
            "skills=%s loaded=%s source=%s embed_skipped=%s top=%s meta_calls=%s hints=%s "
            "route_ms=%s eligible_calls=%s unevaluated=%s initial_cover=%s oracle_cover=%s "
            "unavailable_routed=%s gate_ms=%s model=%s vision=%s",
            chat_key,
            mode,
            self.offered,
            self.spec_bytes,
            steps,
            called,
            self.unknown_calls,
            self.not_offered,
            final_forced,
            self.markup_stripped,
            self.retries,
            end,
            usage.input_tokens,
            usage.output_tokens,
            int(ms),
            *[
                (skill_fields or {}).get(key, "-")
                for key in (
                    "skills",
                    "loaded",
                    "source",
                    "embed_skipped",
                    "top",
                    "meta_calls",
                    "hints",
                    "route_ms",
                    "eligible_calls",
                    "unevaluated",
                    "initial_cover",
                    "oracle_cover",
                    "unavailable_routed",
                    "gate_ms",
                )
            ],
            model or "-",
            "-" if vision is None else ("on" if vision else "off"),
        )
