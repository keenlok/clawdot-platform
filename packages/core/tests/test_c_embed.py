from chatcore.embed import DOC_PREFIX, QUERY_PREFIX, FakeEmbedder, FastEmbedder


async def test_fake_embedder_is_deterministic_and_similar_texts_are_close() -> None:
    embedder = FakeEmbedder(dims=128)
    first = await embedder.embed_query("hotel in Tokyo")
    repeated = await embedder.embed_query("hotel in Tokyo")
    related = await embedder.embed_documents(["Tokyo hotel booking"])
    unrelated = await embedder.embed_query("apples in basket")

    assert first == repeated
    assert len(first) == 128
    assert sum(a * b for a, b in zip(first, related[0], strict=True)) > sum(
        a * b for a, b in zip(first, unrelated, strict=True)
    )


async def test_fast_embedder_applies_model_query_and_document_prefixes(
    monkeypatch, tmp_path
) -> None:
    calls = []

    class StubEmbedding:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def embed(self, texts):
            calls.append(("docs", texts))
            return [[1.0, 0.0]]

        def query_embed(self, text):
            calls.append(("query", text))
            return [[0.0, 1.0]]

    monkeypatch.setattr("fastembed.TextEmbedding", StubEmbedding)
    embedder = FastEmbedder(str(tmp_path))

    await embedder.embed_documents(["hello"])
    await embedder.embed_query("hello")

    assert calls[1] == ("docs", [DOC_PREFIX + "hello"])
    assert calls[2] == ("query", QUERY_PREFIX + "hello")
