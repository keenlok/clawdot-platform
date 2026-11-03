import asyncio
import math
import sys
import threading
from types import SimpleNamespace

import pytest
from chatcore.embed import FakeEmbedder, FastEmbedder
from chatcore.skill_router import SkillRouter
from chatcore.skills import Skill, SkillSet
from chatcore.tools.registry import ToolRegistry


class Plugin:
    name = "router"

    def tools(self):
        return []

    def skills(self):
        return [
            Skill(
                "reminders",
                "Reminders",
                ("set a reminder", "提醒我", "remind me", "job ping"),
                (),
                keywords=("remind", "提醒"),
            ),
            Skill(
                "expenses",
                "Expenses",
                ("record expense", "split bill", "expense entry", "记账"),
                (),
                keywords=("expense", "bill"),
            ),
            Skill(
                "pages",
                "Read web pages",
                ("read page", "open a website", "web page text", "website contents"),
                (),
                keywords=("page",),
            ),
        ]


@pytest.fixture
def skills():
    plugin = Plugin()
    return SkillSet(
        ToolRegistry(),
        {skill.name: (skill, plugin.name) for skill in plugin.skills()},
        {},
        {},
        (),
        (),
    )


async def test_keyword_routing_rejects_ascii_prefix_words(skills):
    router = SkillRouter(skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05)
    result = await router.route("reminderz")
    assert not result.hits


async def test_keyword_routing_matches_chinese_substrings(skills):
    router = SkillRouter(skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05)
    result = await router.route("请提醒我")
    assert [hit.skill for hit in result.hits] == ["reminders"]


async def test_keyword_routing_casefolds_ascii_words(skills):
    router = SkillRouter(skills, None, mode="keyword", timeout_ms=100, threshold=0.5, margin=0.05)
    result = await router.route("REMIND ME")
    assert [hit.skill for hit in result.hits] == ["reminders"]
    assert result.embed_skipped == "disabled"


async def test_keywords_are_unique_and_ordered_before_embedding_hits(skills):
    class FixedEmbedder:
        dims = 2
        in_flight = 0
        document_calls = 0

        async def embed_documents(self, texts):
            self.document_calls += 1
            return [[1.0, 0.0] for _ in texts]

        async def embed_query(self, text):
            return [1.0, 0.0]

    router = SkillRouter(
        skills, FixedEmbedder(), mode="embed", timeout_ms=100, threshold=0.5, margin=0.05
    )
    await router.warm()
    result = await router.route("REMIND me and reminder, expense and bill")
    names = [hit.skill for hit in result.hits]
    assert names == ["reminders", "expenses", "pages"]
    assert len(names) == len(set(names))
    assert all(hit.source == "keyword" for hit in result.hits[:2])
    assert result.hits[2].source == "embed"


async def test_embedding_threshold_margin_max_example_and_vector_normalisation():
    class ThreeSkills:
        name = "three"

        def tools(self):
            return []

        def skills(self):
            return [
                Skill(
                    f"skill_{i}",
                    f"Skill {i}",
                    (f"example_{i}_a", f"example_{i}_b", f"example_{i}_c", f"example_{i}_d"),
                    (),
                )
                for i in range(3)
            ]

    plugin = ThreeSkills()
    catalog = SkillSet(
        ToolRegistry(), {item.name: (item, plugin.name) for item in plugin.skills()}, {}, {}, (), ()
    )

    class Vectors:
        in_flight = 0
        document_calls = 0

        def __init__(self, scale):
            self.scale = scale

        async def embed_documents(self, _texts):
            self.document_calls += 1
            return [[0.0, 1.0]]

        async def embed_query(self, text):
            if text == "no keywords":
                return [1.0, 0.0]
            values = {
                "skill_0": (0.9, 1.0),
                "skill_1": (0.85, 2.0),
                "skill_2": (0.8, 3.0),
            }
            prefix = text.split("_", 2)
            skill = f"skill_{prefix[1]}"
            example = prefix[2]
            score, scale = values[skill]
            if example == "a":
                return [score * scale * self.scale, math.sqrt(1 - score**2) * scale * self.scale]
            return [0.0, 10.0 * self.scale]

    async def names_for(scale, margin, threshold):
        router = SkillRouter(
            catalog,
            Vectors(scale),
            mode="embed",
            timeout_ms=100,
            threshold=threshold,
            margin=margin,
        )
        await router.warm()
        return router, await router.route("no keywords")

    base_router, all_three = await names_for(1, 0.11, 0.7)
    assert [hit.skill for hit in all_three.hits] == ["skill_0", "skill_1", "skill_2"]
    assert all_three.top[0][1] == pytest.approx(0.9)
    _, top_two = await names_for(1, 0.04, 0.7)
    assert [hit.skill for hit in top_two.hits] == ["skill_0", "skill_1"]
    _, below = await names_for(1, 0.11, 0.91)
    assert not below.hits
    _, scaled = await names_for(100, 0.11, 0.7)
    assert [hit.skill for hit in scaled.hits] == [hit.skill for hit in all_three.hits]
    assert base_router._vectors["skill_0"][0] != [0.9, 0.1]


async def test_example_vectors_use_query_embedding_path(skills):
    class DifferentPaths:
        in_flight = 0

        def __init__(self):
            self.document_calls = 0
            self.query_texts = []

        async def embed_documents(self, texts):
            self.document_calls += 1
            return [[0.0, 1.0] for _ in texts]

        async def embed_query(self, text):
            self.query_texts.append(text)
            return [1.0, 0.0]

    embedder = DifferentPaths()
    router = SkillRouter(skills, embedder, mode="embed", timeout_ms=100, threshold=0.9, margin=0)
    await router.warm()
    assert embedder.document_calls == 0
    assert embedder.query_texts == [
        example for name in skills.names() for example in skills.get(name).examples
    ]
    result = await router.route("unmatched")
    assert result.top[0][1] == pytest.approx(1.0)


async def test_concurrent_routes_admit_only_one_embedding_job(skills):
    class Slow(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.submissions = 0

        async def embed_query(self, text):
            if text not in {
                example for name in skills.names() for example in skills.get(name).examples
            }:
                self.submissions += 1
                self.started.set()
                await self.release.wait()
                return await super().embed_query(text)
            return await super().embed_query(text)

    embedder = Slow()
    router = SkillRouter(skills, embedder, mode="embed", timeout_ms=1000, threshold=0, margin=0)
    await router.warm()
    first = asyncio.create_task(router.route("first no keyword"))
    await embedder.started.wait()
    second = await router.route("second no keyword")
    assert second.embed_skipped == "busy"
    assert embedder.submissions == 1
    embedder.release.set()
    await first


async def test_embedding_busy_skips_without_submitting(skills):
    class BusyEmbedder(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.in_flight = 1
            self.queries = 0

        async def embed_documents(self, texts):
            return await super().embed_documents(texts)

        async def embed_query(self, text):
            self.queries += 1
            return await super().embed_query(text)

    embedder = BusyEmbedder()
    router = SkillRouter(skills, embedder, mode="embed", timeout_ms=100, threshold=0.5, margin=0.05)
    await router.warm()
    embedder.queries = 0
    result = await router.route("remind me")
    assert result.embed_skipped == "busy"
    assert embedder.queries == 0


@pytest.mark.parametrize("kind", ["disabled", "none", "keyword"])
async def test_disabled_embedding_never_creates_warm_task(skills, kind):
    class DisabledEmbedder:
        in_flight = 0

    embedder = None if kind == "none" else DisabledEmbedder()
    mode = "keyword" if kind == "keyword" else "embed"
    router = SkillRouter(skills, embedder, mode=mode, timeout_ms=100, threshold=0.5, margin=0.05)
    result = await router.route("REMIND me")
    assert result.embed_skipped == "disabled"
    assert [hit.skill for hit in result.hits] == ["reminders"]
    assert router._warm_task is None


async def test_initial_route_reports_warming_then_warm_embeddings(skills):
    class SlowWarm(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed_query(self, text):
            if text.startswith("set a reminder"):
                self.started.set()
                await self.release.wait()
            return await super().embed_query(text)

    embedder = SlowWarm()
    router = SkillRouter(skills, embedder, mode="embed", timeout_ms=100, threshold=0.0, margin=0.0)
    first = await router.route("REMIND me")
    assert first.embed_skipped == "warming"
    assert [hit.skill for hit in first.hits] == ["reminders"]
    await embedder.started.wait()
    embedder.release.set()
    await router.warm()
    second = await router.route("no matching keywords")
    assert second.embed_skipped == "-"
    assert second.top


async def test_query_embedding_exception_logs_type_without_input(skills, caplog):
    class Broken(FakeEmbedder):
        async def embed_query(self, text):
            if text == "REMIND SENTINEL-USER-QUERY":
                raise RuntimeError("private query payload")
            return await super().embed_query(text)

    caplog.set_level("WARNING")
    router = SkillRouter(skills, Broken(), mode="embed", timeout_ms=100, threshold=0.5, margin=0.05)
    await router.warm()
    result = await router.route("REMIND SENTINEL-USER-QUERY")
    assert [hit.skill for hit in result.hits] == ["reminders"]
    warnings = [record for record in caplog.records if "skill route failed" in record.getMessage()]
    assert len(warnings) == 1
    assert "SENTINEL-USER-QUERY" not in warnings[0].getMessage()
    assert warnings[0].args == ("RuntimeError",)


async def test_fake_embedding_router_admits_top_two_and_close_scores(skills):
    router = SkillRouter(
        skills, FakeEmbedder(), mode="embed", timeout_ms=1000, threshold=0, margin=0
    )
    await router.warm()
    result = await router.route("remind me")
    assert result.hits
    assert result.hits[0].source == "keyword"
    assert result.top


async def test_timed_out_embedding_keeps_worker_busy_until_job_finishes(skills):
    class SlowEmbedder(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.queries = 0

        async def embed_query(self, text):
            examples = {example for name in skills.names() for example in skills.get(name).examples}
            if text in examples:
                return self._vector(text)
            self.queries += 1
            self.in_flight += 1
            self.started.set()
            try:
                await self.release.wait()
                return self._vector(text)
            finally:
                self.in_flight -= 1

    embedder = SlowEmbedder()
    router = SkillRouter(skills, embedder, mode="embed", timeout_ms=10, threshold=0.5, margin=0.05)
    await router.warm()
    result = await router.route("plain words")
    assert result.embed_skipped == "timeout"
    assert embedder.queries == 1
    busy = await router.route("another request")
    assert busy.embed_skipped == "busy"
    assert embedder.queries == 1
    embedder.release.set()
    await asyncio.sleep(0.01)
    await router.route("one more")
    assert embedder.queries == 2


async def test_fast_embedder_counter_tracks_executor_after_cancel(monkeypatch, tmp_path):
    class SlowModel:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def query_embed(self, _text):
            self.started.set()
            self.release.wait(2)
            return iter(([1.0, 2.0],))

        def embed(self, _texts):
            self.started.set()
            self.release.wait(2)
            return iter(([1.0, 2.0],))

    model = SlowModel()
    monkeypatch.setitem(
        sys.modules, "fastembed", SimpleNamespace(TextEmbedding=lambda **_kwargs: model)
    )
    embedder = FastEmbedder(str(tmp_path))
    query = asyncio.create_task(embedder.embed_query("safe test"))
    assert await asyncio.to_thread(model.started.wait, 1)
    assert embedder.in_flight == 1
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query
    assert embedder.in_flight == 1
    model.release.set()
    for _ in range(100):
        if embedder.in_flight == 0:
            break
        await asyncio.sleep(0.01)
    assert embedder.in_flight == 0
    model.started.clear()
    model.release.clear()
    documents = asyncio.create_task(embedder.embed_documents(["safe example"]))
    assert await asyncio.to_thread(model.started.wait, 1)
    assert embedder.in_flight == 1
    model.release.set()
    await documents
    assert embedder.in_flight == 0
    model.started.clear()
    model.release.clear()
    concurrent = [asyncio.create_task(embedder.embed_query(f"query {i}")) for i in range(25)]
    assert await asyncio.to_thread(model.started.wait, 1)
    await asyncio.sleep(0.01)
    assert embedder.in_flight == len(concurrent)
    model.release.set()
    await asyncio.gather(*concurrent)
    assert embedder.in_flight == 0
    await asyncio.to_thread(embedder._pool.shutdown, wait=True)
