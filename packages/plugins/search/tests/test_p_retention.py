from datetime import UTC, datetime

from chatcore.events import ChatRef
from chatcore.store.base import Stores
from chatcore.testing import memory_stores
from chatcore_search import SearchPlugin


class Embedder:
    async def embed_documents(self, texts):
        return [[0.0] for _ in texts]


class Search:
    supports_knn = False

    def __init__(self):
        self.deletes = []

    async def delete(self, index, *, chat, query=None):
        self.deletes.append((index, chat, query))
        return 1


class DB:
    async def fetch(self, sql, *args):
        assert "SELECT DISTINCT chat_key FROM messages" in sql
        return [{"chat_key": "fake:db-chat"}]


async def test_search_retention_deletes_each_known_chat_including_buffer_chats():
    plugin = SearchPlugin(Embedder())
    buffered = ChatRef("fake", "buffered")
    plugin._pending[(buffered, None)] = object()
    stores = memory_stores()
    search = Search()
    stores.search = search
    stores.db = DB()
    cutoff = datetime(2025, 10, 30, tzinfo=UTC)
    await plugin.purge_older_than(cutoff, stores)
    assert {chat.key for _, chat, _ in search.deletes} == {"fake:db-chat", buffered.key}
    assert all(
        query["range"]["ts_end"]["lt"] == cutoff.isoformat() for _, _, query in search.deletes
    )


async def test_search_retention_is_noop_without_search_index():
    plugin = SearchPlugin(Embedder())
    await plugin.purge_older_than(
        datetime.now(UTC), Stores(repo=memory_stores().repo, cache=memory_stores().cache)
    )
