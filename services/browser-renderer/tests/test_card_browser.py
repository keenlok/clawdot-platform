from __future__ import annotations

import struct

import pytest
from chatcore_browser_renderer.engine import RendererEngine
from chatcore_browser_renderer.settings import RendererSettings
from playwright.async_api import async_playwright


class BrowserProbe:
    def __init__(self, browser):
        self.browser = browser
        self.title = None
        self.route_actions = []
        self.row_bottoms = []
        self.clips = []

    async def new_context(self, **kwargs):
        context = await self.browser.new_context(**kwargs)
        return ContextProbe(context, self)


class ContextProbe:
    def __init__(self, context, probe):
        self.context = context
        self.probe = probe

    async def route(self, pattern, handler):
        async def capture(route, request=None):
            await handler(RouteProbe(route, self.probe), request)

        await self.context.route(pattern, capture)

    async def new_page(self):
        return PageProbe(await self.context.new_page(), self.probe)

    async def close(self):
        await self.context.close()


class RouteProbe:
    def __init__(self, route, probe):
        self.route = route
        self.probe = probe

    @property
    def request(self):
        return self.route.request

    async def abort(self):
        self.probe.route_actions.append("abort")
        await self.route.abort()

    async def continue_(self):
        self.probe.route_actions.append("continue")
        await self.route.continue_()


class PageProbe:
    def __init__(self, page, probe):
        self.page = page
        self.probe = probe

    def __getattr__(self, name):
        return getattr(self.page, name)

    async def set_content(self, *args, **kwargs):
        await self.page.set_content(*args, **kwargs)
        self.probe.title = await self.page.title()

    def locator(self, selector):
        return LocatorProbe(self.page.locator(selector), self.probe, selector)

    async def screenshot(self, **kwargs):
        self.probe.clips.append(kwargs["clip"])
        return await self.page.screenshot(**kwargs)


class LocatorProbe:
    def __init__(self, locator, probe, selector):
        self.locator = locator
        self.probe = probe
        self.selector = selector

    async def evaluate(self, *args, **kwargs):
        return await self.locator.evaluate(*args, **kwargs)

    async def count(self):
        return await self.locator.count()

    def nth(self, index):
        return LocatorProbe(self.locator.nth(index), self.probe, self.selector)

    async def bounding_box(self):
        box = await self.locator.bounding_box()
        if box and self.selector == "tr[data-row]":
            self.probe.row_bottoms.append(box["y"] + box["height"])
        return box


def _png_dimensions(png):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", png[16:24])


@pytest.mark.browser
async def test_card_png_dimensions_and_script_does_not_run():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        probe = BrowserProbe(browser)
        engine = RendererEngine(RendererSettings(), browser=probe)
        result = await engine.render_card(
            '<!doctype html><title>safe</title><script>document.title="changed"</script>'
            '<section data-block style="height:40px">Card</section>',
            "light",
        )
        await browser.close()
    assert result.status == "ok" and len(result.pages) == 1
    assert _png_dimensions(result.pages[0])[0] == 1000
    assert _png_dimensions(result.pages[0])[1] <= 9000
    assert probe.title == "safe"


@pytest.mark.browser
async def test_card_aborts_external_image_and_css_requests():
    markup = (
        "<style>.x{display:block;width:20px;height:20px;"
        "background-image:url(https://example.invalid/y)}</style>"
        '<img src="https://example.invalid/x"><section class="x" data-block>Card</section>'
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        probe = BrowserProbe(browser)
        engine = RendererEngine(RendererSettings(), browser=probe)
        result = await engine.render_card(markup, "light")
        await browser.close()
    assert result.status == "ok"
    assert "continue" not in probe.route_actions
    assert probe.route_actions and set(probe.route_actions) == {"abort"}


@pytest.mark.browser
async def test_card_long_table_pages_are_row_aligned_and_png_bounded():
    rows = "".join(
        f"<tr data-row><td>{index} " + ("long cell text " * 25) + "</td></tr>"
        for index in range(40)
    )
    markup = f"<table><tbody>{rows}</tbody></table>"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        probe = BrowserProbe(browser)
        engine = RendererEngine(
            RendererSettings(card_page_css_px=600, card_max_pages=10), browser=probe
        )
        result = await engine.render_card(markup, "light")
        await browser.close()
    assert result.status == "ok" and len(result.pages) > 1
    assert all(
        width == 1000 and height <= 9000 for width, height in map(_png_dimensions, result.pages)
    )
    cuts = [clip["y"] + clip["height"] for clip in probe.clips[:-1]]
    assert cuts and all(
        any(abs(cut - bottom) < 0.01 for bottom in probe.row_bottoms) for cut in cuts
    )
