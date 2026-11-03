"""Elasticsearch SearchIndex backend with chat-scoped queries and Python RRF."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from chatcore.events import ChatRef
from chatcore.store.base import SearchHit


class EsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    es_url: str = "http://127.0.0.1:59200"
    es_api_key: SecretStr | None = None
    search_index_prefix: str = "sb-bot-"


def rrf_fuse(rankings: Sequence[Sequence[str]], *, k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class EsSearchIndex:
    supports_knn = True

    def __init__(
        self,
        client: Any | None = None,
        *,
        es_url: str | None = None,
        es_api_key: SecretStr | None = None,
        index_prefix: str | None = None,
    ) -> None:
        settings = EsSettings()
        if client is None:
            url = es_url or settings.es_url
            key = es_api_key or settings.es_api_key
            from elasticsearch import AsyncElasticsearch

            client = AsyncElasticsearch(
                url,
                api_key=key.get_secret_value() if key else None,
            )
        self.client = client
        self.index_prefix = settings.search_index_prefix if index_prefix is None else index_prefix

    def index_name(self, index: str) -> str:
        return f"{self.index_prefix}{index}"

    @staticmethod
    def _filters(chat: ChatRef, filters: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = [{"term": {"chat_key": chat.key}}]
        for field, value in (filters or {}).items():
            if isinstance(value, Mapping) and value.get("not") is not None:
                result.append({"bool": {"must_not": [{"term": {field: value["not"]}}]}})
            elif isinstance(value, Mapping) and ("gte" in value or "lte" in value):
                result.append({"range": {field: dict(value)}})
            elif isinstance(value, list):
                result.append({"terms": {field: value}})
            else:
                result.append({"term": {field: value}})
        return result

    async def index(
        self, index: str, doc_id: str, doc: Mapping[str, Any], *, chat: ChatRef
    ) -> None:
        await self.client.index(
            index=self.index_name(index),
            id=doc_id,
            document={**doc, "chat_key": chat.key},
            refresh=False,
        )

    async def _search_text(self, index: str, chat: ChatRef, text: str, filters, size: int):
        return await self.client.search(
            index=self.index_name(index),
            size=size,
            query={
                "bool": {
                    "must": [{"multi_match": {"query": text, "fields": ["text", "text.cjk"]}}],
                    "filter": self._filters(chat, filters),
                }
            },
            highlight={"fields": {"text": {}}},
        )

    async def _search_vector(self, index: str, chat: ChatRef, vector, filters, size: int):
        return await self.client.search(
            index=self.index_name(index),
            knn={
                "field": "embedding",
                "query_vector": list(vector),
                "k": size,
                "num_candidates": max(100, size * 10),
                "filter": {"bool": {"filter": self._filters(chat, filters)}},
            },
            size=size,
        )

    @staticmethod
    def _hits(response: Mapping[str, Any]) -> list[SearchHit]:
        hits = response.get("hits", {}).get("hits", [])
        return [
            SearchHit(
                doc_id=str(hit["_id"]),
                score=float(hit.get("_score") or 0),
                source=hit.get("_source", {}),
                highlights=tuple(hit.get("highlight", {}).get("text", [])),
            )
            for hit in hits
        ]

    async def search(
        self,
        index: str,
        *,
        chat: ChatRef,
        text: str | None = None,
        vector: Sequence[float] | None = None,
        filters: Mapping[str, Any] | None = None,
        size: int = 10,
    ) -> list[SearchHit]:
        if text is None and vector is None:
            return []
        if text is not None and vector is not None:
            text_response, vector_response = await asyncio.gather(
                self._search_text(index, chat, text, filters, size),
                self._search_vector(index, chat, vector, filters, size),
            )
            text_hits, vector_hits = self._hits(text_response), self._hits(vector_response)
            fused = rrf_fuse(
                [[hit.doc_id for hit in text_hits], [hit.doc_id for hit in vector_hits]]
            )
            by_id = {hit.doc_id: hit for hit in [*text_hits, *vector_hits]}
            return [
                SearchHit(doc_id, score, by_id[doc_id].source, by_id[doc_id].highlights)
                for doc_id, score in fused[:size]
            ]
        response = (
            await self._search_text(index, chat, text, filters, size)
            if text is not None
            else await self._search_vector(index, chat, vector, filters, size)
        )
        return self._hits(response)

    async def delete(
        self, index: str, *, chat: ChatRef, query: Mapping[str, Any] | None = None
    ) -> int:
        filters = [{"term": {"chat_key": chat.key}}]
        if query:
            filters.append(dict(query))
        response = await self.client.delete_by_query(
            index=self.index_name(index),
            query={"bool": {"filter": filters}},
            refresh=False,
            conflicts="proceed",
        )
        return int(response.get("deleted", 0))

    async def ensure_templates(self) -> None:
        concrete_index = f"{self.index_prefix}chunks-v1"
        template = {
            "index_patterns": [f"{concrete_index}*"],
            "template": {
                "aliases": {f"{self.index_prefix}chunks": {"is_write_index": True}},
                "mappings": {
                    "dynamic": False,
                    "properties": {
                        "chat_key": {"type": "keyword"},
                        "kind": {"type": "keyword"},
                        "user_keys": {"type": "keyword"},
                        "ts_start": {"type": "date"},
                        "ts_end": {"type": "date"},
                        "trip_id": {"type": "keyword"},
                        "source_url": {"type": "keyword", "index": False},
                        "file_id": {"type": "keyword"},
                        "message_ids": {"type": "keyword"},
                        "thread_id": {"type": "keyword"},
                        "text": {
                            "type": "text",
                            "analyzer": "standard",
                            "fields": {"cjk": {"type": "text", "analyzer": "cjk"}},
                        },
                        "embedding": {
                            "type": "dense_vector",
                            "dims": 768,
                            "similarity": "cosine",
                            "index": True,
                            "index_options": {"type": "bbq_hnsw"},
                        },
                    },
                },
            },
            "priority": 200,
        }
        await self.client.indices.put_index_template(
            name=f"{self.index_prefix}chunks-v1", **template
        )
        memories_index = f"{self.index_prefix}memories-v1"
        memories_template = {
            "index_patterns": [memories_index],
            "template": {
                "aliases": {f"{self.index_prefix}memories": {"is_write_index": True}},
                "mappings": {
                    "dynamic": False,
                    "properties": {
                        "chat_key": {"type": "keyword"},
                        "subject_user_key": {"type": "keyword"},
                        "source": {"type": "keyword"},
                        "memory_id": {"type": "long"},
                        "pinned": {"type": "boolean"},
                        "created_at": {"type": "date"},
                        "text": {
                            "type": "text",
                            "analyzer": "standard",
                            "fields": {"cjk": {"type": "text", "analyzer": "cjk"}},
                        },
                        "embedding": {
                            "type": "dense_vector",
                            "dims": 768,
                            "similarity": "cosine",
                            "index": True,
                            "index_options": {"type": "bbq_hnsw"},
                        },
                    },
                },
            },
            "priority": 200,
        }
        await self.client.indices.put_index_template(name=memories_index, **memories_template)
        # Create the concrete indices now, so the aliases exist before the bot writes; a write
        # to a missing alias would auto-create an index the templates don't match.
        for name in (concrete_index, memories_index):
            if await self.client.indices.exists(index=name):
                continue
            try:
                await self.client.indices.create(index=name)
            except Exception as exc:
                if (
                    getattr(exc, "status_code", None) != 400
                    or getattr(exc, "body", {}).get("error", {}).get("type")
                    != "resource_already_exists_exception"
                ):
                    raise
