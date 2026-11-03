from __future__ import annotations

import socket
from uuid import uuid4

import pytest
from chatcore.events import ChatRef
from chatcore.store.es import EsSearchIndex, rrf_fuse


def _es_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 59200), timeout=0.2):
            return True
    except OSError:
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _es_available(), reason="Elasticsearch is not running")
async def test_es_templates_scoping_and_hybrid_rrf() -> None:
    prefix = f"sb-bot-test-{uuid4().hex[:8]}-"
    concrete = f"{prefix}chunks-v1"
    template_name = f"{prefix}chunks-v1"
    index = EsSearchIndex(es_url="http://127.0.0.1:59200", index_prefix=prefix)
    chat_a, chat_b = ChatRef("tg", "integration-a"), ChatRef("tg", "integration-b")
    options = index.client.options(ignore_status=404)
    try:
        await index.ensure_templates()
        mapping_response = await index.client.indices.get_mapping(index=concrete)
        mapping = mapping_response[concrete]["mappings"]
        properties = mapping["properties"]
        assert mapping["dynamic"] in (False, "false")
        assert properties["embedding"]["type"] == "dense_vector"
        assert properties["embedding"]["dims"] == 768
        assert properties["embedding"]["index_options"]["type"] == "bbq_hnsw"
        assert properties["text"]["fields"]["cjk"]["analyzer"] == "cjk"
        assert properties["thread_id"]["type"] == "keyword"

        await index.index(
            "chunks",
            "a-1",
            {
                "kind": "message_window",
                "text": "Tokyo hotel reserved for Alice",
                "embedding": [0.1] * 768,
                "message_ids": ["a-1"],
                "user_keys": ["tg:alice"],
            },
            chat=chat_a,
        )
        await index.index(
            "chunks",
            "a-2",
            {
                "kind": "message_window",
                "text": "Tokyo accommodation booking",
                "embedding": [0.08] * 768,
                "message_ids": ["a-2"],
                "user_keys": ["tg:alice"],
            },
            chat=chat_a,
        )
        await index.index(
            "chunks",
            "b-1",
            {
                "kind": "message_window",
                "text": "Tokyo hotel private from chat B",
                "embedding": [0.1] * 768,
                "message_ids": ["b-1"],
                "user_keys": ["tg:bob"],
            },
            chat=chat_b,
        )
        await index.client.indices.refresh(index=concrete)

        bm25_a = await index.search("chunks", chat=chat_a, text="Tokyo hotel", size=10)
        vector_a = await index.search("chunks", chat=chat_a, vector=[0.1] * 768, size=10)
        hybrid_a = await index.search(
            "chunks", chat=chat_a, text="Tokyo hotel", vector=[0.1] * 768, size=10
        )
        assert bm25_a and vector_a and hybrid_a
        assert "b-1" not in {hit.doc_id for hit in bm25_a}
        assert "b-1" not in {hit.doc_id for hit in vector_a}
        assert "b-1" not in {hit.doc_id for hit in hybrid_a}
        expected = [
            doc_id
            for doc_id, _ in rrf_fuse(
                [[hit.doc_id for hit in bm25_a], [hit.doc_id for hit in vector_a]]
            )
        ]
        assert [hit.doc_id for hit in hybrid_a] == expected

        await index.delete("chunks", chat=chat_a)
        remaining = await index.search("chunks", chat=chat_b, text="private", size=5)
        assert [hit.doc_id for hit in remaining] == ["b-1"]
    finally:
        await options.indices.delete(index=concrete)
        await options.indices.delete_index_template(name=template_name)
        # ensure_templates() also creates the memories index and template.
        await options.indices.delete(index=f"{prefix}memories-v1")
        await options.indices.delete_index_template(name=f"{prefix}memories-v1")
        await index.client.close()
