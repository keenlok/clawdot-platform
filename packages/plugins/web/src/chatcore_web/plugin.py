"""Search the public web and read pages returned by a human or search result."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
from urllib.parse import urlsplit, urlunsplit

import httpx
import trafilatura
from chatcore.plugin import BasePlugin
from chatcore.security.net_guard import BlockedURL, FetchError, failure_cause, safe_get
from chatcore.tools.registry import Tool, ToolContext, ToolResult
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from chatcore_web.settings import WebSettings

from . import maps

# Pinned to the package name so log records keep their pre-split logger name.
logger = logging.getLogger("chatcore_web")


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    port = parts.port
    authority = f"[{host}]" if ":" in host else host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        authority += f":{port}"
    return urlunsplit((scheme, authority, parts.path or "/", parts.query, ""))


def _cache_key(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"web:{prefix}:{digest}"


def _extract(content_type: str, body: bytes) -> tuple[str | None, str]:
    mime = content_type.lower()
    if "pdf" in mime:
        reader = PdfReader(io.BytesIO(body))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:50])
        title = reader.metadata.title if reader.metadata else None
        return title, text
    decoded = body.decode("utf-8", errors="replace")
    if "html" in mime or "xhtml" in mime:
        text = trafilatura.extract(decoded, include_comments=False, include_tables=True) or ""
        title = trafilatura.extract_metadata(decoded)
        return (title.title if title and title.title else None), text
    return None, decoded


class WebPlugin(BasePlugin):
    name = "web"

    def __init__(self, settings: WebSettings | None = None) -> None:
        self.settings = settings or WebSettings()

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="search_web",
                description=(
                    "Find one quick fact or URLs. Not for multi-source or comparison questions → "
                    "research."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"description": "Search text or question.", "type": "string"},
                        "max_results": {
                            "description": "Result count; default 5, configured maximum applies.",
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=self.search_web,
            ),
            Tool(
                name="fetch_web_page",
                description=(
                    "Read one URL posted in this turn or returned by search. Google Maps and "
                    "share.google links return the place name and coordinates."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {
                            "description": "Posted or search-returned HTTP(S) URL.",
                            "type": "string",
                        }
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                handler=self.fetch_web_page,
            ),
        ]

    async def search_web(self, ctx: ToolContext, query: str, max_results: int = 5) -> ToolResult:
        normalized_query = re.sub(r"\s+", " ", query).strip().casefold()
        key = _cache_key("search", normalized_query)
        cached = await ctx.stores.cache.get(key)
        if cached:
            results = json.loads(cached)
        else:
            count = max(1, min(max_results, self.settings.max_results))
            try:
                api_key = self.settings.tavily_api_key.get_secret_value()
                async with httpx.AsyncClient(timeout=self.settings.fetch_timeout_s) as client:
                    response = await client.post(
                        "https://api.tavily.com/search",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={"query": query, "max_results": count},
                    )
                    response.raise_for_status()
                payload = response.json()
                results = [
                    {
                        "title": str(row.get("title", ""))[:300],
                        "url": str(row["url"]),
                        "snippet": str(row.get("content", ""))[:300],
                    }
                    for row in payload.get("results", [])[:count]
                    if isinstance(row, dict) and row.get("url")
                ]
                await ctx.stores.cache.set(key, json.dumps(results).encode(), ttl_s=3600)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("web search failed error=%s", type(exc).__name__)
                return ToolResult.error("Web search is temporarily unavailable.")
        for result in results:
            try:
                ctx.turn.seen_urls.add(_normalize_url(result["url"]))
            except (TypeError, ValueError):
                continue
        content = "\n".join(f"{row['title']}\n{row['url']}\n{row['snippet']}" for row in results)
        return ToolResult(content=content or "No results found.", untrusted=True)

    async def fetch_web_page(self, ctx: ToolContext, url: str) -> ToolResult:
        try:
            requested = urlsplit(url.strip())
            if requested.username is not None or requested.password is not None:
                return ToolResult.error("Links containing credentials cannot be opened.")
            normalized = _normalize_url(url)
        except (ValueError, UnicodeError):
            return ToolResult.error("That link is invalid.")
        seen_urls: set[str] = set()
        for seen_url in ctx.turn.seen_urls:
            try:
                seen_urls.add(_normalize_url(seen_url))
            except (ValueError, UnicodeError):
                continue
        if normalized not in seen_urls:
            return ToolResult.error(
                "I can only open links that someone posted or that came from a search."
            )
        if ctx.turn.fetches >= self.settings.max_fetches_per_turn:
            return ToolResult.error("This turn has reached its page-fetch limit.")
        ctx.turn.fetches += 1
        key = _cache_key("page", normalized)
        cached = await ctx.stores.cache.get(key)
        if cached:
            page = json.loads(cached)
            return ToolResult(content=page["content"], untrusted=True)
        host = urlsplit(normalized).hostname or ""
        try:
            if maps.is_map_link(normalized):
                place = await maps.resolve_map_link(
                    normalized, timeout_s=self.settings.fetch_timeout_s
                )
                if place is not None:
                    content = maps.format_place(place)
                    page = {
                        "final_url": normalized,
                        "title": "Google Maps link",
                        "text": content,
                        "content": content,
                    }
                    await ctx.stores.cache.set(key, json.dumps(page).encode(), ttl_s=6 * 3600)
                    logger.info("map link resolved host=%s kind=%s", host, place.kind)
                    return ToolResult(content=content, untrusted=True)
            fetched = await safe_get(
                normalized,
                max_bytes=self.settings.fetch_max_bytes,
                timeout_s=self.settings.fetch_timeout_s,
                allowed_types=("text/*", "application/pdf", "application/json"),
            )
            if not 200 <= fetched.status < 300:
                logger.warning(
                    "web page returned non-success host=%s status=%d",
                    urlsplit(fetched.final_url).hostname or "",
                    fetched.status,
                )
                return ToolResult.error(f"The page returned HTTP {fetched.status}.")
            title, text = await asyncio.to_thread(_extract, fetched.content_type, fetched.body)
            text_truncated = len(text) > self.settings.fetch_max_chars
            page = {
                "final_url": fetched.final_url,
                "title": title or "Untitled page",
                "text": text[: self.settings.fetch_max_chars],
            }
            page["content"] = f"Title: {page['title']}\nURL: {page['final_url']}\n\n{page['text']}"
            if fetched.truncated or text_truncated:
                page["content"] += "\n\n[Truncated]"
            await ctx.stores.cache.set(key, json.dumps(page).encode(), ttl_s=6 * 3600)
            return ToolResult(content=page["content"], untrusted=True)
        except BlockedURL as exc:
            logger.info(
                "web fetch blocked host=%s error=%s",
                host,
                type(exc).__name__,
            )
            return ToolResult.error(str(exc))
        except FetchError as exc:
            logger.info(
                "web fetch failed host=%s error=%s cause=%s",
                host,
                type(exc).__name__,
                failure_cause(exc),
            )
            return ToolResult.error(str(exc))
        except (httpx.HTTPError, ValueError, OSError, PdfReadError) as exc:
            logger.warning(
                "web extraction failed host=%s error=%s",
                urlsplit(url).hostname or "",
                type(exc).__name__,
            )
            return ToolResult.error("The page could not be read.")
