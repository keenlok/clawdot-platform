from __future__ import annotations

import json

import httpx
import pytest
import respx
from chatcore.plugin import Plugin
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext
from chatcore_stocks import StocksPlugin, StocksSettings


def context() -> ToolContext:
    return ToolContext(GROUP, user(), False, FakePlatform(), memory_stores())


def plugin() -> StocksPlugin:
    return StocksPlugin(StocksSettings(api_base="https://stocks.test/api"))


@pytest.mark.parametrize(
    ("tool", "path", "params", "arguments"),
    [
        ("stock_analysis", "/analysis/AAPL", {"lang": "en"}, {"ticker": "AAPL"}),
        ("stock_digest", "/digests/latest", {"lang": "en"}, {}),
        ("stock_prices", "/stocks/AAPL/prices", {"days": "30"}, {"ticker": "AAPL"}),
        (
            "stock_list",
            "/analysis",
            {"page": "1", "page_size": "20", "sector": "Tech"},
            {"sector": "Tech"},
        ),
    ],
)
@respx.mock
async def test_endpoints_trim_and_cache(
    tool: str, path: str, params: dict[str, str], arguments: dict[str, str]
) -> None:
    row = {
        "ticker": "AAPL",
        "analysis_date": "2026-10-01",
        "recommendation": "hold",
        "summary": "Stored research",
        "secret_noise": "drop me",
    }
    response_data = {"results": [row], "page": 1, "total": 1} if tool == "stock_list" else row
    route = respx.get(f"https://stocks.test/api{path}").mock(
        return_value=httpx.Response(200, json=response_data)
    )
    subject = plugin()
    ctx = context()
    result = await getattr(subject, tool)(ctx, **arguments)
    payload = json.loads(result.content)
    assert result.untrusted
    assert "not a live quote" in payload["notice"]
    assert payload["stored_analysis_date"] == "2026-10-01"
    assert "secret_noise" not in result.content
    assert "AAPL" in result.content
    assert dict(route.calls[0].request.url.params) == params
    cached = await getattr(subject, tool)(ctx, **arguments)
    assert cached.content == result.content
    assert len(route.calls) == 1


@pytest.mark.parametrize("ticker", ["../x", "A/B", "A%2F", "", "TOO-LONG-TICKER"])
async def test_rejects_invalid_ticker_without_request(ticker: str) -> None:
    result = await plugin().stock_analysis(context(), ticker)
    assert result.is_error


@respx.mock
async def test_normalizes_lowercase_ticker() -> None:
    route = respx.get("https://stocks.test/api/analysis/AAPL", params={"lang": "en"}).mock(
        return_value=httpx.Response(200, json={"ticker": "AAPL", "date": "2026-10-01"})
    )
    result = await plugin().stock_analysis(context(), " aapl ")
    assert result.untrusted
    assert route.called


@pytest.mark.parametrize("lang", ["EN", "english", "en_US", "e", "en-us-extra", ""])
async def test_rejects_invalid_language(lang: str) -> None:
    analysis = await plugin().stock_analysis(context(), "AAPL", lang)
    digest = await plugin().stock_digest(context(), lang)
    assert analysis.is_error
    assert digest.is_error


@respx.mock
async def test_accepts_language_with_region() -> None:
    route = respx.get("https://stocks.test/api/digests/latest", params={"lang": "en-US"}).mock(
        return_value=httpx.Response(200, json={"date": "2026-10-01", "title": "Digest"})
    )
    result = await plugin().stock_digest(context(), "en-US")
    assert result.untrusted
    assert route.called


@pytest.mark.parametrize(
    ("tool", "path", "params", "arguments"),
    [
        ("stock_analysis", "/analysis/MISSING", {"lang": "en"}, {"ticker": "MISSING"}),
        ("stock_digest", "/digests/latest", {"lang": "en"}, {}),
        ("stock_prices", "/stocks/MISSING/prices", {"days": "30"}, {"ticker": "MISSING"}),
        ("stock_list", "/analysis", {"page": "1", "page_size": "20"}, {}),
    ],
)
@respx.mock
async def test_404_returns_no_analysis(
    tool: str, path: str, params: dict[str, str], arguments: dict[str, str]
) -> None:
    route = respx.get(f"https://stocks.test/api{path}", params=params).mock(
        return_value=httpx.Response(404)
    )
    ctx = context()
    subject = plugin()
    result = await getattr(subject, tool)(ctx, **arguments)
    assert result.untrusted
    assert "No analysis" in result.content or "No stored stock analysis" in result.content
    cached = await getattr(subject, tool)(ctx, **arguments)
    assert cached.content == result.content
    assert len(route.calls) == 1


async def test_plugins_register_and_expose_object_schemas() -> None:
    web = __import__("chatcore_web").WebPlugin(
        __import__("chatcore_web").WebSettings(tavily_api_key="test-placeholder-key")
    )
    for subject in (web, plugin()):
        assert isinstance(subject, Plugin)
        assert subject.name in {"web", "stocks"}
        assert all(tool.parameters["type"] == "object" for tool in subject.tools())
    stock_list = next(tool for tool in plugin().tools() if tool.name == "stock_list")
    assert stock_list.parameters["properties"]["sector"] == {
        "type": "string",
        "description": "Optional sector filter.",
    }
    assert stock_list.parameters["properties"]["recommendation"] == {
        "type": "string",
        "description": "Optional recommendation filter.",
    }
