from __future__ import annotations

import io
import json
import threading

import httpx
import pytest
import respx
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext
from chatcore_web import WebPlugin, WebSettings, _extract, _normalize_url
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def context() -> ToolContext:
    return ToolContext(GROUP, user(), False, FakePlatform(), memory_stores())


def plugin() -> WebPlugin:
    return WebPlugin(WebSettings(tavily_api_key="test-placeholder-key"))


def test_normalization_never_keeps_url_userinfo() -> None:
    assert _normalize_url("https://example.com@evil.com/a") == "https://evil.com/a"


@respx.mock
async def test_search_auth_shape_and_provenance() -> None:
    route = respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Example", "url": "https://example.com/a#part", "content": "snippet"}
                ]
            },
        )
    )
    ctx = context()
    result = await plugin().search_web(ctx, "example", 7)
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer test-placeholder-key"
    assert "test-placeholder-key" not in str(request.url)
    assert request.read() == b'{"query":"example","max_results":5}'
    assert "https://example.com/a" in ctx.turn.seen_urls
    assert result.untrusted
    cached = await plugin().search_web(ctx, " EXAMPLE  ", 2)
    assert cached.content == result.content
    assert len(route.calls) == 1


async def test_fetch_refuses_unprovenanced_url() -> None:
    result = await plugin().fetch_web_page(context(), "https://example.com")
    assert result.is_error
    assert result.content == "I can only open links that someone posted or that came from a search."


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a?q=2",
        "https://example.com/a",
        "https://example.com/b?q=1",
        "https://evil.example.com/a?q=1",
        "https://example.com.evil.com/a?q=1",
        "http://example.com/a?q=1",
        "https://example.com@evil.com/a?q=1",
        "https://example.com:8443/a?q=1",
    ],
)
async def test_provenance_refusals_do_not_fetch_or_touch_cache(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    from chatcore_web import plugin as chatcore_web

    ctx = context()
    ctx.turn.seen_urls.add("HTTPS://Example.COM:443/a?q=1#frag")
    calls = {"network": 0, "cache": 0}

    async def fake_get(*args, **kwargs):
        calls["network"] += 1
        raise AssertionError("refused URL reached the network")

    original_get = ctx.stores.cache.get

    async def cache_get(key: str):
        calls["cache"] += 1
        return await original_get(key)

    monkeypatch.setattr(chatcore_web, "safe_get", fake_get)
    monkeypatch.setattr(ctx.stores.cache, "get", cache_get)
    result = await plugin().fetch_web_page(ctx, url)
    assert result.is_error
    assert ctx.turn.fetches == 0
    assert calls == {"network": 0, "cache": 0}


@pytest.mark.parametrize(
    "url",
    ["https://example.com/a?q=1", "https://EXAMPLE.com./a?q=1#other"],
)
async def test_normalizes_raw_provenance_and_fetches_normalized_url(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    from chatcore.security.net_guard import FetchResult
    from chatcore_web import plugin as chatcore_web

    ctx = context()
    ctx.turn.seen_urls.add("HTTPS://Example.COM:443/a?q=1#frag")
    fetched_urls: list[str] = []

    async def fake_get(requested_url: str, **kwargs):
        fetched_urls.append(requested_url)
        return FetchResult(requested_url, 200, "text/plain", b"page", False)

    monkeypatch.setattr(chatcore_web, "safe_get", fake_get)
    result = await plugin().fetch_web_page(ctx, url)
    assert result.untrusted
    assert fetched_urls == ["https://example.com/a?q=1"]
    assert ctx.turn.fetches == 1


async def test_extraction_runs_in_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatcore.security.net_guard import FetchResult
    from chatcore_web import plugin as chatcore_web

    ctx = context()
    ctx.turn.seen_urls.add("https://example.com/")
    main_thread = threading.get_ident()
    extraction_threads: list[int] = []

    async def fake_get(*args, **kwargs):
        return FetchResult("https://example.com/", 200, "text/html", b"html", False)

    def fake_extract(content_type: str, body: bytes) -> tuple[str, str]:
        extraction_threads.append(threading.get_ident())
        return "title", "text"

    monkeypatch.setattr(chatcore_web, "safe_get", fake_get)
    monkeypatch.setattr(chatcore_web, "_extract", fake_extract)
    result = await plugin().fetch_web_page(ctx, "https://example.com/")
    assert result.untrusted
    assert extraction_threads and extraction_threads[0] != main_thread


@pytest.mark.parametrize("status", [404, 500])
async def test_http_error_pages_are_not_extracted_or_cached(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    from chatcore.security.net_guard import FetchResult
    from chatcore_web import plugin as chatcore_web

    ctx = context()
    ctx.turn.seen_urls.add("https://example.com/")

    async def fake_get(*args, **kwargs):
        return FetchResult("https://example.com/", status, "text/html", b"error page", False)

    def unexpected_extract(*args, **kwargs):
        raise AssertionError("non-success HTTP body must not be extracted")

    monkeypatch.setattr(chatcore_web, "safe_get", fake_get)
    monkeypatch.setattr(chatcore_web, "_extract", unexpected_extract)
    result = await plugin().fetch_web_page(ctx, "https://example.com/")
    assert result.is_error
    assert result.content == f"The page returned HTTP {status}."
    assert (
        await ctx.stores.cache.get(chatcore_web._cache_key("page", "https://example.com/")) is None
    )


async def test_fetch_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = plugin()
    ctx = context()
    ctx.turn.seen_urls.add("https://example.com/")
    ctx.turn.fetches = 3

    async def no_fetch(*args, **kwargs):
        raise AssertionError("fetch should not be called")

    monkeypatch.setattr("chatcore_web.plugin.safe_get", no_fetch)
    result = await instance.fetch_web_page(ctx, "https://example.com/")
    assert result.is_error


def test_extracts_html_pdf_and_json() -> None:
    html = (
        b"<html><head><title>Page title</title></head><body><article>"
        b"<p>Readable text.</p></article></body></html>"
    )
    title, text = _extract("text/html", html)
    assert title == "Page title"
    assert "Readable text" in text

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Tiny PDF fixture) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    assert "Tiny PDF fixture" in _extract("application/pdf", buffer.getvalue())[1]
    assert _extract("application/json", b'{"answer": 42}')[1] == '{"answer": 42}'


@pytest.mark.parametrize(
    ("body", "body_truncated", "limit"),
    [(b"abcdefgh", False, 5), (b"abc", True, 50)],
)
async def test_fetch_truncates_and_caches(
    monkeypatch: pytest.MonkeyPatch, body: bytes, body_truncated: bool, limit: int
) -> None:
    from chatcore.security.net_guard import FetchResult
    from chatcore_web import plugin as chatcore_web

    calls = 0

    async def fake_get(*args, **kwargs):
        nonlocal calls
        calls += 1
        return FetchResult("https://example.com/", 200, "text/plain", body, body_truncated)

    monkeypatch.setattr(chatcore_web, "safe_get", fake_get)
    instance = WebPlugin(WebSettings(tavily_api_key="test-placeholder-key", fetch_max_chars=limit))
    ctx = context()
    ctx.turn.seen_urls.add("https://example.com/")
    first = await instance.fetch_web_page(ctx, "https://example.com/")
    assert first.untrusted and first.content.endswith("\n\n[Truncated]")
    second = await instance.fetch_web_page(ctx, "https://example.com/")
    assert second.content == first.content
    assert calls == 1


async def test_provenanced_share_link_resolves_formats_caches_and_counts_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from chatcore_web import plugin as chatcore_web
    from chatcore_web.maps import MapPlace

    ctx = context()
    url = "https://share.google/abc"
    ctx.turn.seen_urls.add(url)
    resolved: list[str] = []

    async def resolve(value: str, *, timeout_s: float):
        resolved.append(value)
        return MapPlace("Ah Hua Cafe", "place", "/g/11abc", None, 1.3, 103.8)

    async def no_fetch(*args, **kwargs):
        raise AssertionError("map result should not fetch HTML")

    monkeypatch.setattr(chatcore_web.maps, "resolve_map_link", resolve)
    monkeypatch.setattr(chatcore_web, "safe_get", no_fetch)
    caplog.set_level("INFO", logger="chatcore_web")
    instance = plugin()
    first = await instance.fetch_web_page(ctx, url)
    assert ctx.turn.fetches == 1
    second = await instance.fetch_web_page(ctx, url)
    assert first.untrusted and "Place: Ah Hua Cafe" in first.content
    assert "Coordinates: 1.30000, 103.80000" in first.content
    assert second.content == first.content
    assert ctx.turn.fetches == 2
    assert resolved == [url]
    record = next(record for record in caplog.records if "map link resolved" in record.message)
    assert record.message == "map link resolved host=share.google kind=place"
    assert "abc" not in record.message and "Ah Hua" not in record.message
    cached = json.loads(await ctx.stores.cache.get(chatcore_web._cache_key("page", url)))
    assert cached["content"] == first.content


async def test_map_link_that_redirects_off_google_falls_through_to_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatcore.security.net_guard import FetchResult
    from chatcore_web import plugin as chatcore_web

    ctx = context()
    url = "https://share.google/abc"
    ctx.turn.seen_urls.add(url)
    calls = []

    async def no_place(*args, **kwargs):
        return None

    async def fetch(value: str, **kwargs):
        calls.append(value)
        return FetchResult(value, 200, "text/plain", b"page", False)

    monkeypatch.setattr(chatcore_web.maps, "resolve_map_link", no_place)
    monkeypatch.setattr(chatcore_web, "safe_get", fetch)
    assert (await plugin().fetch_web_page(ctx, url)).untrusted
    assert calls == [url]


async def test_unprovenanced_map_link_is_refused_without_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatcore_web import plugin as chatcore_web

    async def unexpected(*args, **kwargs):
        raise AssertionError("provenance gate must run before maps")

    monkeypatch.setattr(chatcore_web.maps, "resolve_map_link", unexpected)
    result = await plugin().fetch_web_page(context(), "https://share.google/abc")
    assert result.is_error


async def test_fetch_logs_safe_cause_and_blocked_class(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatcore.security.net_guard import BlockedURL, FetchError
    from chatcore_web import plugin as chatcore_web

    ctx = context()
    ctx.turn.seen_urls.add("https://example.com/private-path?secret=value")

    async def fail(*args, **kwargs):
        try:
            raise httpx.ConnectError("hidden details")
        except httpx.ConnectError as cause:
            raise FetchError("The site could not be reached.") from cause

    monkeypatch.setattr(chatcore_web, "safe_get", fail)
    caplog.set_level("INFO", logger="chatcore_web")
    await plugin().fetch_web_page(ctx, "https://example.com/private-path?secret=value")
    record = next(record for record in caplog.records if "web fetch failed" in record.message)
    assert record.message == "web fetch failed host=example.com error=FetchError cause=ConnectError"
    assert "private-path" not in record.message and "secret" not in record.message

    async def blocked(*args, **kwargs):
        raise BlockedURL("blocked")

    monkeypatch.setattr(chatcore_web, "safe_get", blocked)
    await plugin().fetch_web_page(ctx, "https://example.com/private-path?secret=value")
    assert any(
        record.message == "web fetch blocked host=example.com error=BlockedURL"
        for record in caplog.records
    )
