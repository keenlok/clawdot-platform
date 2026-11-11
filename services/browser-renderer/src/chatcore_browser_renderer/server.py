from __future__ import annotations

import base64
import json
import logging
import time
from urllib.parse import urlsplit

from aiohttp import web
from chatcore.security.net_guard import BlockedURL, parse_https_destination

from .engine import CardRenderResult, RendererEngine, RenderResult
from .settings import RendererSettings

LOGGER = logging.getLogger("chatcore_browser_renderer")
MAX_REQUEST_BYTES = 4096
RENDERER_KEY = web.AppKey("renderer", RendererEngine)


def _blocked() -> web.Response:
    return web.json_response(RenderResult("blocked_url").as_dict(), status=200, dumps=json.dumps)


def create_app(
    settings: RendererSettings | None = None, engine: RendererEngine | None = None
) -> web.Application:
    config = settings or RendererSettings()
    renderer = engine or RendererEngine(config)
    app = web.Application(client_max_size=config.card_max_html_bytes + 1024)
    app[RENDERER_KEY] = renderer

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def render(request: web.Request) -> web.Response:
        if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
            raise web.HTTPBadRequest(text="Malformed request.")
        if request.content_type != "application/json":
            return _blocked()
        try:
            raw = await request.read()
            if len(raw) > MAX_REQUEST_BYTES:
                raise web.HTTPBadRequest(text="Malformed request.")
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, web.HTTPRequestEntityTooLarge):
            raise web.HTTPBadRequest(text="Malformed request.") from None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"url"}
            or not isinstance(payload["url"], str)
        ):
            return _blocked()
        url = payload["url"]
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() != "https":
                return _blocked()
            host, _ = parse_https_destination(url)
        except (BlockedURL, ValueError):
            return _blocked()
        started = time.monotonic()
        try:
            result = await renderer.render(url)
        except Exception:
            result = RenderResult("error")
        elapsed = time.monotonic() - started
        LOGGER.info(
            "render host=%s status=%s duration_ms=%d bytes=%d",
            host,
            result.status,
            int(elapsed * 1000),
            len(result.text.encode()),
        )
        return web.json_response(result.as_dict(), status=200, dumps=json.dumps)

    async def card(request: web.Request) -> web.Response:
        started = time.monotonic()
        html_bytes = pages = png_bytes = 0
        status = "invalid"
        if request.content_type != "application/json":
            return web.json_response({"status": "invalid"}, status=200, dumps=json.dumps)
        try:
            raw = await request.read()
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, web.HTTPRequestEntityTooLarge):
            payload = None
        if (
            isinstance(payload, dict)
            and set(payload) == {"html", "theme", "width"}
            and isinstance(payload["html"], str)
            and isinstance(payload["theme"], str)
            and payload["theme"] in {"light", "dark"}
            and type(payload["width"]) is int
            and payload["width"] == 400
        ):
            html_value = payload["html"]
            html_bytes = len(html_value.encode("utf-8"))
            if html_bytes <= config.card_max_html_bytes:
                try:
                    result = await renderer.render_card(html_value, payload["theme"])
                except Exception:
                    result = CardRenderResult("error", [])
                if result is not None:
                    status = result.status
                    pages = len(result.pages)
                    png_bytes = sum(map(len, result.pages))
                    LOGGER.info(
                        "card status=%s duration_ms=%d html_bytes=%d pages=%d png_bytes=%d",
                        status,
                        int((time.monotonic() - started) * 1000),
                        html_bytes,
                        pages,
                        png_bytes,
                    )
                    if status == "ok":
                        return web.json_response(
                            {
                                "status": "ok",
                                "pages": [
                                    base64.b64encode(p).decode("ascii") for p in result.pages
                                ],
                            },
                            status=200,
                            dumps=json.dumps,
                        )
                    return web.json_response({"status": status}, status=200, dumps=json.dumps)
            else:
                status = "too_large"
        LOGGER.info(
            "card status=%s duration_ms=%d html_bytes=%d pages=%d png_bytes=%d",
            status,
            int((time.monotonic() - started) * 1000),
            html_bytes,
            pages,
            png_bytes,
        )
        return web.json_response({"status": status}, status=200, dumps=json.dumps)

    app.router.add_get("/healthz", health)
    app.router.add_post("/render", render)
    app.router.add_post("/card", card)
    app.on_startup.append(lambda _app: renderer.start())
    app.on_cleanup.append(lambda _app: renderer.close())
    return app
