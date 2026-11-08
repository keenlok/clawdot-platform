from uuid import uuid4

import pytest
from chatcore.events import ChatRef
from chatcore.store.es import EsSearchIndex, rrf_fuse


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.indices = FakeIndices(self.calls)

    async def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        return {"hits": {"hits": [{"_id": "a", "_score": 2.0, "_source": {"text": "a"}}]}}

    async def index(self, **kwargs):
        self.calls.append(("index", kwargs))

    async def delete_by_query(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return {"deleted": 1}


class FakeIndices:
    def __init__(self, calls):
        self.calls = calls
        self.present = False
        self.created: set[str] = set()

    async def put_index_template(self, **kwargs):
        self.calls.append(("template", kwargs))

    async def exists(self, **kwargs):
        self.calls.append(("exists", kwargs))
        return self.present or kwargs["index"] in self.created

    async def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        self.created.add(kwargs["index"])

    async def refresh(self, **kwargs):
        self.calls.append(("refresh", kwargs))


async def test_es_backend_scopes_every_query_and_write_to_required_chat() -> None:
    client = FakeClient()
    backend = EsSearchIndex(client=client)
    chat = ChatRef("tg", "A")

    await backend.search("chunks", chat=chat, text="hello")
    await backend.index("chunks", "doc", {"text": "hello"}, chat=chat)
    await backend.delete("chunks", chat=chat, query={"term": {"user_keys": "tg:1"}})

    assert all("tg:A" in str(call[1]) for call in client.calls)
    assert client.calls[1][1]["refresh"] is False
    assert client.calls[2][1]["query"]["bool"]["filter"][0] == {"term": {"chat_key": "tg:A"}}


async def test_es_delete_can_refresh_before_delete_by_query():
    client = FakeClient()
    backend = EsSearchIndex(client=client, index_prefix="test-")

    await backend.delete("memories", chat=ChatRef("tg", "A"), refresh_first=True)

    assert client.calls[0] == ("refresh", {"index": "test-memories"})
    assert client.calls[1][0] == "delete"


def test_rrf_fusion_combines_ranks() -> None:
    result = rrf_fuse([["a", "b"], ["b", "a"]])

    assert result[0][0] == "a"
    assert result[0][1] == pytest.approx(1 / 61 + 1 / 62)
    assert result[1][1] == pytest.approx(1 / 61 + 1 / 62)


async def test_ensure_templates_creates_concrete_mapped_write_index() -> None:
    client = FakeClient()
    backend = EsSearchIndex(client=client, index_prefix="test-")

    await backend.ensure_templates()

    template = client.calls[0][1]
    mapping = template["template"]["mappings"]
    assert mapping["dynamic"] is False
    assert mapping["properties"]["thread_id"] == {"type": "keyword"}
    assert template["template"]["aliases"]["test-chunks"]["is_write_index"] is True
    assert ("create", {"index": "test-chunks-v1"}) in client.calls
    assert ("create", {"index": "test-memories-v1"}) in client.calls

    await backend.ensure_templates()
    assert sum(call == "create" for call, _ in client.calls) == 2


def test_es_backend_uses_settings_prefix_when_no_prefix_is_given(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_INDEX_PREFIX", "isolated-")
    backend = EsSearchIndex(client=FakeClient())

    assert backend.index_name("chunks") == "isolated-chunks"


@pytest.mark.integration
async def test_real_es_delete_refreshes_an_unrefreshed_document():
    from elasticsearch import AsyncElasticsearch

    client = AsyncElasticsearch("http://127.0.0.1:59200")
    try:
        try:
            await client.info()
        except Exception:
            pytest.skip("Elasticsearch is not reachable")
        prefix = f"memory-delete-{uuid4().hex[:12]}-"
        backend = EsSearchIndex(client=client, index_prefix=prefix)
        index = backend.index_name("memories")
        await client.indices.create(
            index=index,
            mappings={
                "properties": {
                    "chat_key": {"type": "keyword"},
                    "memory_id": {"type": "integer"},
                    "text": {"type": "text"},
                }
            },
        )
        chat = ChatRef("test", uuid4().hex)
        try:
            await backend.index("memories", "1", {"memory_id": 1, "text": "ephemeral"}, chat=chat)
            deleted = await backend.delete(
                "memories",
                chat=chat,
                query={"term": {"memory_id": 1}},
                refresh_first=True,
            )
            assert deleted == 1
            await client.indices.refresh(index=index)
            response = await client.search(index=index, query={"term": {"memory_id": 1}})
            assert response["hits"]["total"]["value"] == 0
        finally:
            await client.indices.delete(index=index, ignore_unavailable=True)
    finally:
        await client.close()
