"""Text embedding backends used by vector search."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

QUERY_PREFIX = "task: search result | query: "
DOC_PREFIX = "title: none | text: "


class Embedder(Protocol):
    dims: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class FakeEmbedder:
    """Stable hashed bag-of-words vectors make tests reflect lexical similarity."""

    def __init__(self, dims: int = 768) -> None:
        self.dims = dims
        self.in_flight = 0

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for word in re.findall(r"\w+", text.casefold()):
            digest = hashlib.sha256(word.encode()).digest()
            slot = int.from_bytes(digest[:4], "big") % self.dims
            vector[slot] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class FastEmbedder:
    """Load the pinned local model and keep CPU inference off the event loop."""

    dims = 768
    model_name = "google/embeddinggemma-300m"
    revision = "5090578d9565bb06545b4552f76e6bc2c93e4a66"

    def __init__(self, cache_dir: str | None = None) -> None:
        from fastembed import TextEmbedding

        cache_dir = cache_dir or os.environ.get("EMBED_CACHE_DIR")
        if not cache_dir or not Path(cache_dir).is_dir():
            raise FileNotFoundError("EMBED_CACHE_DIR must point to the downloaded model snapshot")
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fastembed")
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self._model = TextEmbedding(
            model_name=self.model_name,
            cache_dir=cache_dir,
            local_files_only=True,
            specific_model_path=cache_dir,
        )

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._run(
            lambda: [list(v) for v in self._model.embed([DOC_PREFIX + text for text in texts])]
        )

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._run(lambda: list(self._model.query_embed(QUERY_PREFIX + text)))
        return list(vectors[0])

    async def _run(self, function):
        with self._in_flight_lock:
            self._in_flight += 1
        try:
            future = self._pool.submit(function)
        except Exception:
            with self._in_flight_lock:
                self._in_flight -= 1
            raise
        future.add_done_callback(self._job_finished)
        return await asyncio.wrap_future(future)

    @property
    def in_flight(self) -> int:
        with self._in_flight_lock:
            return self._in_flight

    def _job_finished(self, _future) -> None:
        with self._in_flight_lock:
            self._in_flight -= 1
