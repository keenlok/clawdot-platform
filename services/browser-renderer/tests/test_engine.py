from __future__ import annotations

import asyncio
import re
import sys
from types import SimpleNamespace

from chatcore_browser_renderer.engine import RendererEngine, browser_environment
from chatcore_browser_renderer.settings import RendererSettings


class Page:
    def __init__(self, *, body: str = "hello", status: int = 200, delay: float = 0):
        self.body = body
        self.status = status
        self.delay = delay
        self.url = "https://example.com/"
        self.frames = [SimpleNamespace(url=self.url)]
        self.events = {}
        self.goto_calls = 0

    def on(self, event, callback):
        self.events[event] = callback

    async def goto(self, url, wait_until):
        self.goto_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        self.url = url
        return SimpleNamespace(status=self.status)

    async def wait_for_load_state(self, state, **kwargs):
        return None

    async def title(self):
        return "A title"

    async def inner_text(self, selector):
        return self.body

    async def close(self):
        self.closed = True


class Context:
    def __init__(self, page):
        self.page = page
        self.closed = False
        self.routes = []

    async def route(self, pattern, callback):
        self.routes.append((pattern, callback))

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class Browser:
    def __init__(self, pages):
        self.pages = list(pages)
        self.contexts = []
        self.closed = False

    async def new_context(self, **options):
        context = Context(self.pages[len(self.contexts)])
        context.options = options
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


async def test_each_render_gets_and_closes_a_fresh_context() -> None:
    browser = Browser([Page(), Page()])
    engine = RendererEngine(RendererSettings(render_settle_s=0), browser=browser)
    first = await engine.render("https://example.com/one")
    second = await engine.render("https://example.com/two")
    assert first.status == second.status == "ok"
    assert len(browser.contexts) == 2
    assert all(context.closed for context in browser.contexts)
    assert browser.contexts[0].options["accept_downloads"] is False
    assert browser.contexts[0].options["service_workers"] == "block"


async def test_output_is_collapsed_stripped_and_truncated() -> None:
    browser = Browser([Page(body="  a\x00  b\n c\x85 d ")])
    engine = RendererEngine(
        RendererSettings(render_settle_s=0, render_max_chars=5), browser=browser
    )
    result = await engine.render("https://example.com/")
    assert result.text == "a b c"
    assert result.truncated is True


async def test_context_closes_when_render_times_out() -> None:
    browser = Browser([Page(delay=0.05)])
    engine = RendererEngine(
        RendererSettings(render_timeout_s=0.01, render_settle_s=0), browser=browser
    )
    result = await engine.render("https://example.com/")
    assert result.status == "timeout"
    assert browser.contexts[0].closed


async def test_browser_recycles_at_configured_render_count() -> None:
    browser = Browser([Page()])
    engine = RendererEngine(
        RendererSettings(render_settle_s=0, render_max_renders_per_browser=1), browser=browser
    )
    await engine.render("https://example.com/")
    assert browser.closed
    assert engine.browser is None


async def test_plain_404_is_an_error() -> None:
    browser = Browser([Page(status=404, body="Not found")])
    engine = RendererEngine(RendererSettings(render_settle_s=0), browser=browser)
    assert (await engine.render("https://example.com/missing")).status == "error"


def test_source_contains_no_user_interaction_calls() -> None:
    source = "\n".join(
        path.read_text()
        for path in __import__("pathlib").Path(__file__).parents[1].joinpath("src").rglob("*.py")
    )
    assert not re.search(
        r"\.(?:click|fill|type|press|dispatch_event|check|select_option|set_input_files)\s*\(",
        source,
    )


async def test_context_closes_and_browser_recycles_after_page_exception() -> None:
    class BrokenPage(Page):
        async def inner_text(self, selector):
            raise RuntimeError("private page text must not escape")

    browser_one = Browser([BrokenPage()])
    browser_two = Browser([Page()])
    created = []

    async def factory():
        browser = browser_one if not created else browser_two
        created.append(browser)
        return browser

    engine = RendererEngine(RendererSettings(render_settle_s=0), browser_factory=factory)
    first = await engine.render("https://example.com/")
    assert first.status == "error"
    assert browser_one.contexts[0].closed
    assert browser_one.closed
    assert engine.browser is None
    second = await engine.render("https://example.com/")
    assert second.status == "ok"
    assert len(created) == 2


async def test_dialog_is_dismissed_and_popup_is_closed() -> None:
    browser = Browser([Page()])
    engine = RendererEngine(RendererSettings(render_settle_s=0), browser=browser)
    await engine.render("https://example.com/")

    class Dialog:
        dismissed = False

        async def dismiss(self):
            self.dismissed = True

    dialog = Dialog()
    popup = Page()
    await browser.contexts[0].page.events["dialog"](dialog)
    await browser.contexts[0].page.events["popup"](popup)
    assert dialog.dismissed
    assert popup.closed


async def test_concurrency_cap_returns_busy_without_queueing() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowPage(Page):
        async def goto(self, url, wait_until):
            self.goto_calls += 1
            started.set()
            await release.wait()
            self.url = url
            return SimpleNamespace(status=200)

    browser = Browser([SlowPage()])
    engine = RendererEngine(
        RendererSettings(render_concurrency=1, render_settle_s=0), browser=browser
    )
    pending = asyncio.create_task(engine.render("https://example.com/"))
    await started.wait()
    busy = await engine.render("https://example.com/second")
    assert busy.status == "busy"
    assert len(browser.contexts) == 1
    release.set()
    assert (await pending).status == "ok"


async def test_concurrent_first_renders_start_one_browser() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    launches = 0

    async def factory():
        nonlocal launches
        launches += 1
        started.set()
        await release.wait()
        return Browser([Page(), Page()])

    engine = RendererEngine(
        RendererSettings(render_concurrency=2, render_settle_s=0), browser_factory=factory
    )
    one = asyncio.create_task(engine.render("https://example.com/1"))
    await started.wait()
    two = asyncio.create_task(engine.render("https://example.com/2"))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(one, two)
    assert launches == 1
    assert [result.status for result in results] == ["ok", "ok"]


async def test_busy_response_does_not_wait_for_browser_startup() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory():
        started.set()
        await release.wait()
        return Browser([Page()])

    engine = RendererEngine(
        RendererSettings(render_concurrency=1, render_settle_s=0), browser_factory=factory
    )
    pending = asyncio.create_task(engine.render("https://example.com/"))
    await started.wait()
    assert (
        await asyncio.wait_for(engine.render("https://example.com/busy"), 0.05)
    ).status == "busy"
    release.set()
    assert (await pending).status == "ok"


async def test_browser_recycling_waits_for_other_active_render() -> None:
    entered_slow = asyncio.Event()
    release_slow = asyncio.Event()

    class SlowPage(Page):
        async def goto(self, url, wait_until):
            if url.endswith("slow"):
                entered_slow.set()
                await release_slow.wait()
            return await super().goto(url, wait_until)

    browser = Browser([SlowPage(), Page()])
    engine = RendererEngine(
        RendererSettings(render_concurrency=2, render_settle_s=0, render_max_renders_per_browser=1),
        browser=browser,
    )
    slow = asyncio.create_task(engine.render("https://example.com/slow"))
    await entered_slow.wait()
    fast = await engine.render("https://example.com/fast")
    assert fast.status == "ok"
    assert not browser.closed
    release_slow.set()
    assert (await slow).status == "ok"
    assert browser.closed


def test_browser_environment_scrubs_hostile_overrides_but_keeps_browser_path() -> None:
    cleaned = browser_environment(
        {
            "PLAYWRIGHT_BROWSERS_PATH": "/tmp/browsers",
            "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK": "1",
            "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
            "NODE_OPTIONS": "--require evil.js",
            "HTTP_PROXY": "http://bad.invalid",
            "https_proxy": "http://bad.invalid",
            "NO_PROXY": "*",
            "PATH": "/usr/bin",
        }
    )
    assert cleaned == {"PLAYWRIGHT_BROWSERS_PATH": "/tmp/browsers", "PATH": "/usr/bin"}


async def test_launch_and_context_force_loopback_through_proxy(monkeypatch) -> None:
    captured = {}
    browser = Browser([Page()])

    class Chromium:
        async def launch(self, **options):
            captured.update(options)
            return browser

    class Manager:
        chromium = Chromium()

        async def stop(self):
            pass

    manager = Manager()

    class Playwright:
        async def start(self):
            return manager

    monkeypatch.setitem(
        sys.modules,
        "playwright.async_api",
        SimpleNamespace(async_playwright=lambda: Playwright()),
    )
    monkeypatch.setenv("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "1")
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid")
    engine = RendererEngine(RendererSettings(render_settle_s=0))
    engine.port = 8765
    assert (await engine.render("https://example.com/")).status == "ok"
    assert "--proxy-bypass-list=<-loopback>" in captured["args"]
    assert "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK" not in captured["env"]
    assert "HTTP_PROXY" not in captured["env"]
    assert browser.contexts[0].options["proxy"]["bypass"] == "<-loopback>"


async def test_blocked_site_is_reported_plainly_and_not_retried() -> None:
    page = Page(status=403, body="Access denied")
    browser = Browser([page])
    engine = RendererEngine(RendererSettings(render_settle_s=0), browser=browser)
    result = await engine.render("https://example.com/")
    assert result.status == "blocked_by_site"
    assert result.text == ""
    assert page.goto_calls == 1


async def test_challenge_frame_returns_empty_blocked_result_after_single_navigation() -> None:
    page = Page(status=200, body="private challenge response")
    page.frames = [
        SimpleNamespace(url="https://example.com/"),
        SimpleNamespace(url="https://newassets.hcaptcha.com/captcha/v1/challenge"),
    ]
    browser = Browser([page])
    engine = RendererEngine(RendererSettings(render_settle_s=0), browser=browser)
    result = await engine.render("https://example.com/")
    assert result.status == "blocked_by_site"
    assert result.text == ""
    assert page.goto_calls == 1


async def test_challenge_url_on_main_document_does_not_trigger_frame_detection() -> None:
    page = Page(body="normal page")
    page.url = "https://www.google.com/recaptcha/api2/anchor"
    page.frames = [SimpleNamespace(url=page.url)]
    browser = Browser([page])
    engine = RendererEngine(RendererSettings(render_settle_s=0), browser=browser)
    result = await engine.render(page.url)
    assert result.status == "ok"


async def test_passive_recaptcha_script_does_not_block_long_article() -> None:
    class BootstrapPage(Page):
        async def goto(self, url, wait_until):
            self.goto_calls += 1
            self.events["request"](
                SimpleNamespace(
                    url="https://www.google.com/recaptcha/api.js",
                    frame=self.main_frame,
                    resource_type="script",
                    is_navigation_request=lambda: False,
                )
            )
            self.url = url
            return SimpleNamespace(status=200)

    page = BootstrapPage(body="Long article content. " * 200)
    page.main_frame = object()
    browser = Browser([page])
    engine = RendererEngine(RendererSettings(render_settle_s=0), browser=browser)
    result = await engine.render("https://example.com/article")
    assert result.status == "ok"


async def test_route_and_connect_refusals_are_reported_separately() -> None:
    class Route:
        async def abort(self):
            pass

    class Request:
        url = "https://blocked.example/image.png"
        resource_type = "image"

    from chatcore_browser_renderer.budget import RenderBudget

    budget = RenderBudget()
    await RendererEngine._route(Route(), Request(), budget)
    budget.refuse("blocked.example:443")
    assert budget.route_refused_authorities == ["blocked.example"]
    assert budget.connect_refused_authorities == ["blocked.example"]


async def test_new_context_does_not_reuse_storage_state() -> None:
    browser = Browser([Page(), Page()])
    engine = RendererEngine(RendererSettings(render_settle_s=0), browser=browser)
    await engine.render("https://example.com/one")
    await engine.render("https://example.com/two")
    assert browser.contexts[0] is not browser.contexts[1]
    assert all("storage_state" not in context.options for context in browser.contexts)


async def test_proxy_refusal_of_top_level_host_becomes_blocked_url() -> None:
    class RefusingProxy:
        policy = SimpleNamespace(overrides=None)

        def register(self, budget):
            self.budget = budget
            return SimpleNamespace(token="lease", budget=budget)

        def unregister(self, lease):
            pass

    class FailedNavigation(Page):
        async def goto(self, url, wait_until):
            self.goto_calls += 1
            self.events["request"](
                SimpleNamespace(
                    url=url,
                    frame=self.main_frame,
                    redirected_from=None,
                    is_navigation_request=lambda: True,
                )
            )
            proxy.budget.refuse("example.com:443")
            raise RuntimeError("tunnel refused")

    page = FailedNavigation()
    page.main_frame = object()
    browser = Browser([page])
    proxy = RefusingProxy()
    engine = RendererEngine(RendererSettings(render_settle_s=0), browser=browser, proxy=proxy)
    result = await engine.render("https://example.com/")
    assert engine.last_connect_refused_authorities == ("example.com",)
    assert result.status == "blocked_url"
    assert page.goto_calls == 1


async def test_redirect_navigation_refusal_returns_blocked_url_without_recycling() -> None:
    class Request:
        def __init__(self, url, frame, redirected_from=None):
            self.url = url
            self.frame = frame
            self.redirected_from = redirected_from

        def is_navigation_request(self):
            return True

    class TrackingProxy:
        policy = SimpleNamespace(overrides=None)

        def register(self, budget):
            self.budget = budget
            return SimpleNamespace(token="lease", budget=budget)

        def unregister(self, lease):
            pass

    class RedirectPage(Page):
        async def goto(self, url, wait_until):
            self.goto_calls += 1
            original = Request(url, self.main_frame)
            redirected = Request("https://blocked.example/", self.main_frame, original)
            self.events["request"](original)
            self.events["request"](redirected)
            proxy.budget.refuse("blocked.example:443")
            raise RuntimeError("navigation refused")

    proxy = TrackingProxy()
    page = RedirectPage()
    page.main_frame = object()
    browser = Browser([page])
    engine = RendererEngine(RendererSettings(render_settle_s=0), browser=browser, proxy=proxy)
    result = await engine.render("https://original.example/")
    assert result.status == "blocked_url"
    assert engine.browser is browser
    assert not browser.closed


async def test_subresource_connect_refusal_keeps_page_ok() -> None:
    class Request:
        url = "https://blocked.example/image.png"
        resource_type = "image"

        def __init__(self, frame):
            self.frame = frame

        def is_navigation_request(self):
            return False

    class TrackingProxy:
        policy = SimpleNamespace(overrides=None)

        def register(self, budget):
            self.budget = budget
            return SimpleNamespace(token="lease", budget=budget)

        def unregister(self, lease):
            pass

    proxy = TrackingProxy()

    class SubresourcePage(Page):
        async def goto(self, url, wait_until):
            self.goto_calls += 1
            self.events["request"](Request(object()))
            proxy.budget.refuse("blocked.example:443")
            self.url = url
            return SimpleNamespace(status=200)

    browser = Browser([SubresourcePage()])
    engine = RendererEngine(RendererSettings(render_settle_s=0), browser=browser, proxy=proxy)
    result = await engine.render("https://original.example/")
    assert result.status == "ok"


async def test_second_layer_aborts_non_https_and_unneeded_resources() -> None:
    class Route:
        def __init__(self):
            self.aborted = False
            self.continued = False

        async def abort(self):
            self.aborted = True

        async def continue_(self):
            self.continued = True

    class Request:
        def __init__(self, url, resource_type):
            self.url = url
            self.resource_type = resource_type

    from chatcore_browser_renderer.budget import RenderBudget

    budget = RenderBudget()
    cases = [
        ("http://example.com/a", "document", True),
        ("https://example.com/image", "image", True),
        ("https://example.com/font", "font", True),
        ("https://example.com/audio", "media", True),
        ("wss://example.com/socket", "websocket", True),
        ("https://example.com/events", "eventsource", True),
        ("https://example.com/script", "script", False),
        ("data:text/plain,hi", "document", False),
        ("file:///etc/passwd", "document", True),
    ]
    for url, resource_type, should_abort in cases:
        route = Route()
        await RendererEngine._route(route, Request(url, resource_type), budget)
        assert route.aborted is should_abort
        assert route.continued is (not should_abort)
    assert "example.com" in budget.refused_authorities
