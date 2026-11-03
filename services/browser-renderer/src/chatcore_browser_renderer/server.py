from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlsplit

from aiohttp import web
from chatcore.security.net_guard import BlockedURL, parse_https_destination

from .engine import RendererEngine, RenderResult
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
    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    app[RENDERER_KEY] = renderer

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def render(request: web.Request) -> web.Response:
        if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
            raise web.HTTPBadRequest(text="Malformed request.")
        if request.content_type != "application/json":
            return _blocked()
        try:
            payload = await request.json(loads=json.loads)
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

    app.router.add_get("/healthz", health)
    app.router.add_post("/render", render)
    app.on_startup.append(lambda _app: renderer.start())
    app.on_cleanup.append(lambda _app: renderer.close())
    return app
