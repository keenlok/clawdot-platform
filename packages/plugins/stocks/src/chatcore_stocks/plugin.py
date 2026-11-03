"""Read stored public stock analysis from the stock-investor API."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import httpx
from chatcore.plugin import BasePlugin
from chatcore.skills import Skill
from chatcore.tools.registry import Tool, ToolContext, ToolResult

from chatcore_stocks.settings import StocksSettings

# Pinned to the package name so log records keep their pre-split logger name.
logger = logging.getLogger("chatcore_stocks")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_TICKER_ERROR = "Ticker must match ^[A-Z][A-Z0-9.-]{0,9}$."
_LANG = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")
_FIELDS = {
    "ticker",
    "symbol",
    "name",
    "company",
    "company_name",
    "sector",
    "industry",
    "date",
    "analysis_date",
    "created_at",
    "updated_at",
    "recommendation",
    "rating",
    "target_price",
    "price_target",
    "summary",
    "analysis",
    "description",
    "rationale",
    "highlights",
    "risks",
    "period",
    "currency",
    "content",
    "results",
    "stocks",
    "total",
    "page",
    "page_size",
    "analysis_text",
    "technical",
    "fundamental",
    "prices",
    "close",
    "open",
    "high",
    "low",
    "volume",
    "change",
    "change_percent",
    "digest",
    "title",
    "items",
}


# Default behaviour text for the `stocks` skill; apps may add to or override it.
STOCKS_INSTRUCTIONS = (
    "Stock tools return stored analysis, not live quotes. Say that the analysis is not "
    "financial advice."
)


def _trim(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: _trim(item) for key, item in value.items() if key.lower() in _FIELDS}
        for key in ("data", "result", "results", "analysis", "digest", "prices", "stocks", "items"):
            if key in value and key not in result:
                nested = _trim(value[key])
                if nested not in (None, {}, []):
                    result[key] = nested
        return result
    if isinstance(value, list):
        return [_trim(item) for item in value[:50]]
    if isinstance(value, str):
        return value[:4000]
    return value if isinstance(value, (int, float, bool)) or value is None else str(value)[:500]


def _analysis_date(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("analysis_date", "date", "updated_at", "created_at"):
            if value.get(key):
                return str(value[key])
        for child in value.values():
            found = _analysis_date(child)
            if found != "unknown date":
                return found
    elif isinstance(value, list):
        for child in value:
            found = _analysis_date(child)
            if found != "unknown date":
                return found
    return "unknown date"


def _cache_key(path: str, params: dict[str, Any]) -> str:
    raw = json.dumps([path, sorted(params.items())], sort_keys=True, separators=(",", ":"))
    return "stocks:" + hashlib.sha256(raw.encode()).hexdigest()


class StocksPlugin(BasePlugin):
    name = "stocks"
    explainer_package = "chatcore_stocks"

    def skills(self):
        return [
            Skill(
                "stocks",
                "Read public stock analysis and prices",
                (
                    "check this stock",
                    "show stock prices",
                    "分析这只股票",
                    "查股票价格",
                    "stock update leh",
                ),
                ("stock_analysis", "stock_digest", "stock_prices", "stock_list"),
                keywords=("stock", "price", "股票", "价格"),
                max_steps=3,
                instructions=STOCKS_INSTRUCTIONS,
            )
        ]

    def __init__(self, settings: StocksSettings | None = None) -> None:
        self.settings = settings or StocksSettings()

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "stock_analysis",
                "Read stored ticker analysis. Stored data, not live quotes; not financial advice.",
                {
                    "type": "object",
                    "properties": {
                        "ticker": {
                            "description": "Ticker format: ^[A-Z][A-Z0-9.-]{0,9}$.",
                            "type": "string",
                        },
                        "lang": {
                            "description": "Language tag such as en or en-US; defaults to en.",
                            "type": "string",
                            "default": "en",
                        },
                    },
                    "required": ["ticker"],
                    "additionalProperties": False,
                },
                self.stock_analysis,
            ),
            Tool(
                "stock_digest",
                "Read the stored stock digest. Stored data, not live quotes; not financial advice.",
                {
                    "type": "object",
                    "properties": {
                        "lang": {
                            "description": "Language tag such as en or en-US; defaults to en.",
                            "type": "string",
                            "default": "en",
                        }
                    },
                    "additionalProperties": False,
                },
                self.stock_digest,
            ),
            Tool(
                "stock_prices",
                "Read stored ticker prices. Stored data, not live quotes; not financial advice.",
                {
                    "type": "object",
                    "properties": {
                        "ticker": {
                            "description": "Ticker format: ^[A-Z][A-Z0-9.-]{0,9}$.",
                            "type": "string",
                        },
                        "days": {
                            "description": "History in days; default 30, range 1–365.",
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 365,
                            "default": 30,
                        },
                    },
                    "required": ["ticker"],
                    "additionalProperties": False,
                },
                self.stock_prices,
            ),
            Tool(
                "stock_list",
                "List stored stock analyses. Stored data, not live quotes; not financial advice.",
                {
                    "type": "object",
                    "properties": {
                        "sector": {"description": "Optional sector filter.", "type": "string"},
                        "recommendation": {
                            "description": "Optional recommendation filter.",
                            "type": "string",
                        },
                    },
                    "additionalProperties": False,
                },
                self.stock_list,
            ),
        ]

    async def _get(
        self, ctx: ToolContext, path: str, params: dict[str, Any], *, ticker: str | None = None
    ) -> ToolResult:
        key = _cache_key(path, params)
        cached = await ctx.stores.cache.get(key)
        if cached:
            return ToolResult(content=cached.decode(), untrusted=True)
        url = f"{self.settings.api_base.rstrip('/')}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, params=params)
            if response.status_code == 404:
                content = (
                    f"No analysis for {ticker}. No stored analysis date is available; "
                    "this is not a live quote."
                    if ticker
                    else "No stored stock analysis is available; no date is available, "
                    "and this is not a live quote."
                )
                await ctx.stores.cache.set(key, content.encode(), ttl_s=900)
                return ToolResult(content=content, untrusted=True)
            response.raise_for_status()
            data = _trim(response.json())
            date = _analysis_date(data)
            payload = {
                "stored_analysis_date": date,
                "notice": "This is stored analysis, not a live quote.",
                "data": data,
            }
            content = json.dumps(payload, ensure_ascii=False)
            await ctx.stores.cache.set(key, content.encode(), ttl_s=900)
            return ToolResult(content=content, untrusted=True)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "stocks request failed host=%s error=%s", httpx.URL(url).host, type(exc).__name__
            )
            return ToolResult.error("Stored stock analysis is temporarily unavailable.")

    async def stock_analysis(self, ctx: ToolContext, ticker: str, lang: str = "en") -> ToolResult:
        ticker = ticker.strip().upper()
        if not _TICKER.fullmatch(ticker):
            return ToolResult.error(_TICKER_ERROR)
        if not _LANG.fullmatch(lang):
            return ToolResult.error("Language must match ^[a-z]{2}(-[A-Za-z]{2})?$")
        return await self._get(ctx, f"analysis/{ticker}", {"lang": lang}, ticker=ticker)

    async def stock_digest(self, ctx: ToolContext, lang: str = "en") -> ToolResult:
        if not _LANG.fullmatch(lang):
            return ToolResult.error("Language must match ^[a-z]{2}(-[A-Za-z]{2})?$")
        return await self._get(ctx, "digests/latest", {"lang": lang})

    async def stock_prices(self, ctx: ToolContext, ticker: str, days: int = 30) -> ToolResult:
        ticker = ticker.strip().upper()
        if not _TICKER.fullmatch(ticker):
            return ToolResult.error(_TICKER_ERROR)
        if not 1 <= days <= 365:
            return ToolResult.error("Days must be between 1 and 365.")
        return await self._get(ctx, f"stocks/{ticker}/prices", {"days": days}, ticker=ticker)

    async def stock_list(
        self, ctx: ToolContext, sector: str | None = None, recommendation: str | None = None
    ) -> ToolResult:
        params: dict[str, Any] = {"page": 1, "page_size": 20}
        if sector:
            params["sector"] = sector
        if recommendation:
            params["recommendation"] = recommendation
        return await self._get(ctx, "analysis", params)
