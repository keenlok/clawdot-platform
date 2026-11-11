from __future__ import annotations

import pytest
from chatcore_browser_renderer.engine import RendererEngine
from chatcore_browser_renderer.settings import RendererSettings


class Locator:
    def __init__(self, boxes=(), height=500):
        self.boxes = boxes
        self.height = height

    async def evaluate(self, _js):
        return self.height

    async def count(self):
        return len(self.boxes)

    def nth(self, index):
        return self

    async def bounding_box(self):
        return self.boxes.pop(0)


class Page:
    def __init__(self, height, boxes, png=b"png"):
        self.height = height
        self.boxes = boxes
        self.clips = []
        self.png = png

    def locator(self, selector):
        return Locator(self.boxes if selector == "tr[data-row]" else [], self.height)

    async def set_content(self, *args, **kwargs):
        pass

    async def screenshot(self, **kwargs):
        self.clips.append(kwargs["clip"])
        return self.png


class Context:
    def __init__(self, page):
        self.page = page
        self.options = None
        self.route_args = None

    async def route(self, *args):
        self.route_args = args

    async def new_page(self):
        return self.page

    async def close(self):
        pass


class Browser:
    def __init__(self, context):
        self.context = context

    async def new_context(self, **kwargs):
        self.context.options = kwargs
        return self.context


@pytest.mark.asyncio
async def test_render_card_uses_isolated_context_and_splits_at_rows():
    boxes = [
        {"y": 0, "height": 50},
        {"y": 50, "height": 50},
        {"y": 100, "height": 50},
        {"y": 150, "height": 50},
        {"y": 200, "height": 50},
    ]
    page = Page(250, boxes)
    context = Context(page)
    engine = RendererEngine(RendererSettings(card_page_css_px=150), browser=Browser(context))
    result = await engine.render_card("<p>card</p>", "dark")
    assert result.status == "ok" and len(result.pages) == 2
    assert [clip["height"] for clip in page.clips] == [150, 100]
    assert context.options["java_script_enabled"] is False
    assert context.options["offline"] is True and context.options["device_scale_factor"] == 2.5
    assert context.options["color_scheme"] == "dark"
    assert context.route_args[0] == "**/*"


@pytest.mark.asyncio
async def test_render_card_obeys_shared_busy_counter():
    engine = RendererEngine(RendererSettings(render_concurrency=1))
    engine.active = 1
    result = await engine.render_card("x", "light")
    assert result.status == "busy"


@pytest.mark.asyncio
async def test_short_card_with_footer_fits_one_page_even_with_early_boundary():
    page = Page(760, [{"y": 700, "height": 20}])
    context = Context(page)
    engine = RendererEngine(RendererSettings(card_max_pages=1), browser=Browser(context))
    result = await engine.render_card("<footer>footer</footer>", "light")
    assert result.status == "ok" and len(result.pages) == 1
    assert page.clips == [{"x": 0, "y": 0, "width": 400, "height": 760}]


@pytest.mark.asyncio
async def test_200px_card_fits_one_page_at_one_page_limit():
    page = Page(200, [])
    engine = RendererEngine(
        RendererSettings(card_page_css_px=200, card_max_pages=1), browser=Browser(Context(page))
    )
    result = await engine.render_card("short", "light")
    assert result.status == "ok" and len(result.pages) == 1


@pytest.mark.asyncio
async def test_page_and_png_caps_and_no_boxes_fallback():
    page = Page(200, [])
    engine = RendererEngine(RendererSettings(card_page_css_px=80), browser=Browser(Context(page)))
    result = await engine.render_card("fallback", "light")
    assert result.status == "ok" and [clip["height"] for clip in page.clips] == [80, 120]

    too_many = Page(300, [])
    engine = RendererEngine(
        RendererSettings(card_page_css_px=80, card_max_pages=2), browser=Browser(Context(too_many))
    )
    assert (await engine.render_card("long", "light")).status == "too_large"

    too_big = Page(10, [], png=b"x" * 9)
    engine = RendererEngine(
        RendererSettings(card_max_png_bytes=8), browser=Browser(Context(too_big))
    )
    assert (await engine.render_card("large png", "light")).status == "too_large"


@pytest.mark.asyncio
async def test_3601px_document_has_two_pages_without_slivers():
    page = Page(3601, [])
    engine = RendererEngine(RendererSettings(card_max_pages=2), browser=Browser(Context(page)))
    result = await engine.render_card("long card", "light")
    assert result.status == "ok" and len(result.pages) == 2
    assert [clip["height"] for clip in page.clips] == [3521, 80]


@pytest.mark.asyncio
async def test_short_final_remainder_moves_cut_to_nearest_valid_row_boundary():
    page = Page(3601, [{"y": 0, "height": 3500}, {"y": 3500, "height": 50}])
    engine = RendererEngine(RendererSettings(card_max_pages=2), browser=Browser(Context(page)))
    result = await engine.render_card("long table", "light")
    assert result.status == "ok" and len(result.pages) == 2
    assert [clip["height"] for clip in page.clips] == [3500, 101]


@pytest.mark.asyncio
async def test_browser_startup_is_inside_card_timeout():
    import asyncio

    async def slow_browser():
        await asyncio.sleep(0.05)
        return Browser(Context(Page(10, [])))

    engine = RendererEngine(RendererSettings(card_timeout_s=0.01), browser_factory=slow_browser)
    assert (await engine.render_card("x", "light")).status == "timeout"
