"""Evaluate labelled message retrieval with BM25, vector and hybrid rankings."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from chatcore.embed import FastEmbedder
from chatcore.events import ChatRef
from chatcore.store.es import EsSearchIndex


async def evaluate(fixture: Path) -> None:
    if not os.environ.get("EMBED_CACHE_DIR"):
        raise SystemExit(
            "Set EMBED_CACHE_DIR to the downloaded model cache before running eval_search.py"
        )
    labels = json.loads(await asyncio.to_thread(fixture.read_text))
    index = EsSearchIndex(es_url=os.environ.get("ES_URL", "http://127.0.0.1:59200"))
    embedder = FastEmbedder()
    totals = {"BM25": 0, "vector": 0, "hybrid": 0}
    try:
        for item in labels:
            chat = ChatRef.parse(item["chat_key"])
            query = item["query"]
            expected = set(item["expected_message_ids"])
            vector = await embedder.embed_query(query)
            bm25, knn, hybrid = await asyncio.gather(
                index.search("chunks", chat=chat, text=query, size=20),
                index.search("chunks", chat=chat, vector=vector, size=20),
                index.search("chunks", chat=chat, text=query, vector=vector, size=20),
            )
            for name, hits in (("BM25", bm25), ("vector", knn), ("hybrid", hybrid)):
                found = {
                    message_id for hit in hits for message_id in hit.source.get("message_ids", [])
                }
                totals[name] += bool(found & expected)
        count = len(labels) or 1
        for name, hits in totals.items():
            print(f"{name}: {hits}/{len(labels)} ({hits / count:.1%})")
    finally:
        await index.client.close()


def main() -> None:
    fixture = Path(__file__).parents[1] / "tests" / "fixtures" / "eval_queries.json"
    asyncio.run(evaluate(fixture))


if __name__ == "__main__":
    main()
