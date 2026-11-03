"""Small, bounded router for the skill catalog."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Literal

from chatcore.embed import Embedder
from chatcore.skills import SkillSet

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RouteHit:
    skill: str
    source: Literal["keyword", "embed"]
    score: float | None = None


@dataclass(frozen=True, slots=True)
class RouteResult:
    hits: tuple[RouteHit, ...]
    top: tuple[tuple[str, float], ...]
    embed_skipped: Literal["busy", "timeout", "disabled", "warming", "-"]
    route_ms: float


class SkillRouter:
    def __init__(
        self,
        skills: SkillSet,
        embedder: Embedder | None,
        *,
        mode: Literal["embed", "keyword"],
        timeout_ms: int,
        threshold: float,
        margin: float,
    ) -> None:
        self.skills = skills
        self.embedder = embedder
        self.mode = mode
        self.timeout_ms = timeout_ms
        self.threshold = threshold
        self.margin = margin
        self._vectors: dict[str, list[list[float]]] | None = None
        self._warm_task: asyncio.Task | None = None
        self._routing_job_active = False

    async def warm(self) -> None:
        if self._vectors is not None:
            return
        if not self._embedding_enabled:
            self._vectors = {}
            return
        if self._warm_task is None:
            self._warm_task = asyncio.create_task(self._compute_examples())
        await self._warm_task

    async def _compute_examples(self) -> None:
        assert self.embedder is not None
        entries = [(name, self.skills.get(name).examples) for name in self.skills.names()]
        result: dict[str, list[list[float]]] = {}
        for name, examples in entries:
            result[name] = [
                _normalise(await self.embedder.embed_query(example)) for example in examples
            ]
        self._vectors = result

    @property
    def _embedding_enabled(self) -> bool:
        return (
            self.mode == "embed"
            and self.embedder is not None
            and type(self.embedder).__name__ != "DisabledEmbedder"
        )

    async def route(self, text: str) -> RouteResult:
        started = time.perf_counter()
        text = text[:500]
        keyword_hits = self._keyword_hits(text)
        skipped: Literal["busy", "timeout", "disabled", "warming", "-"] = "-"
        embed_hits: list[RouteHit] = []
        top: tuple[tuple[str, float], ...] = ()
        if not self._embedding_enabled:
            skipped = "disabled"
        elif self._vectors is None:
            if self._warm_task is None:
                self._warm_task = asyncio.create_task(self._compute_examples())
            skipped = "warming"
            if self._warm_task.done():
                try:
                    await self._warm_task
                except Exception as exc:
                    log.warning("skill route failed error=%s", type(exc).__name__)
                    self._vectors = {}
                    skipped = "-"
        elif self._routing_job_active or getattr(self.embedder, "in_flight", 0) > 0:
            skipped = "busy"
        else:
            try:
                self._routing_job_active = True
                query = asyncio.create_task(self.embedder.embed_query(text))
                query.add_done_callback(_consume_exception)
                query.add_done_callback(self._routing_job_finished)
                vector = await asyncio.wait_for(asyncio.shield(query), self.timeout_ms / 1000)
                query = _normalise(vector)
                scores = sorted(
                    ((name, _max_dot(query, examples)) for name, examples in self._vectors.items()),
                    key=lambda item: -item[1],
                )
                top = tuple((name, score) for name, score in scores[:3])
                admitted = [item for item in scores if item[1] >= self.threshold]
                if admitted:
                    best = admitted[0][1]
                    selected = admitted[:2]
                    selected.extend(item for item in admitted[2:] if best - item[1] <= self.margin)
                    embed_hits = [RouteHit(name, "embed", score) for name, score in selected]
            except TimeoutError:
                skipped = "timeout"
            except Exception as exc:
                self._routing_job_active = False
                log.warning("skill route failed error=%s", type(exc).__name__)
                skipped = "-"
        hits = list(keyword_hits)
        seen = {hit.skill for hit in hits}
        hits.extend(hit for hit in embed_hits if hit.skill not in seen)
        return RouteResult(tuple(hits), top, skipped, (time.perf_counter() - started) * 1000)

    def _routing_job_finished(self, _task: asyncio.Task) -> None:
        self._routing_job_active = False

    def _keyword_hits(self, text: str) -> list[RouteHit]:
        folded = text.casefold()
        hits = []
        for name in self.skills.names():
            skill = self.skills.get(name)
            for keyword in skill.keywords:
                if keyword.isascii() and keyword.isalnum():
                    found = re.search(rf"\b{re.escape(keyword.casefold())}\b", folded)
                elif keyword.isascii():
                    found = re.search(rf"(?<!\w){re.escape(keyword.casefold())}(?!\w)", folded)
                else:
                    found = keyword.casefold() in folded
                if found:
                    hits.append(RouteHit(name, "keyword"))
                    break
        return hits


def _consume_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    with contextlib.suppress(BaseException):
        task.exception()


def _max_cosine(query: list[float], examples: list[list[float]]) -> float:
    return _max_dot(_normalise(query), [_normalise(example) for example in examples])


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else [0.0] * len(vector)


def _max_dot(query: list[float], examples: list[list[float]]) -> float:
    return max(
        (sum(a * b for a, b in zip(query, example, strict=False)) for example in examples),
        default=0.0,
    )
