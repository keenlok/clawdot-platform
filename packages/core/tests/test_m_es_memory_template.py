from chatcore.store.es import EsSearchIndex


class TemplateClient:
    def __init__(self) -> None:
        self.templates = []
        self.created = []
        self.indices = self

    async def put_index_template(self, **kwargs):
        self.templates.append(kwargs)

    async def exists(self, **kwargs):
        return kwargs["index"] in self.created

    async def create(self, **kwargs):
        self.created.append(kwargs["index"])


async def test_ensure_templates_adds_chat_scoped_memory_mapping():
    client = TemplateClient()
    backend = EsSearchIndex(client=client, index_prefix="test-")

    await backend.ensure_templates()

    template = next(item for item in client.templates if item["name"] == "test-memories-v1")
    body = template["template"]
    fields = body["mappings"]["properties"]
    assert template["index_patterns"] == ["test-memories-v1"]
    assert body["aliases"]["test-memories"]["is_write_index"] is True
    assert fields["text"]["fields"]["cjk"]["analyzer"] == "cjk"
    assert fields["embedding"]["dims"] == 768
    assert fields["embedding"]["index_options"] == {"type": "bbq_hnsw"}


async def test_ensure_templates_creates_both_concrete_indices():
    client = TemplateClient()
    await EsSearchIndex(client=client, index_prefix="test-").ensure_templates()
    assert client.created == ["test-chunks-v1", "test-memories-v1"]
    await EsSearchIndex(client=client, index_prefix="test-").ensure_templates()
    assert client.created == ["test-chunks-v1", "test-memories-v1"]
