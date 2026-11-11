from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from chatcore_browser_renderer.engine import RenderResult
from chatcore_browser_renderer.server import create_app
from chatcore_browser_renderer.settings import RendererSettings
from pydantic import SecretStr


class FakeEngine:
    def __init__(self):
        self.urls: list[str] = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def render(self, url):
        self.urls.append(url)
        return RenderResult("ok", final_url=url, title="Title", text="Text")

    async def render_card(self, html, theme):
        from chatcore_browser_renderer.engine import CardRenderResult

        return CardRenderResult("ok", [b"\x89PNG\r\n\x1a\nimage"])


async def dispatch(
    app, method: str, path: str, body: bytes = b"", content_type: str = "application/json"
):
    payload = StreamReader(protocol=Mock(), limit=8192)
    if body:
        payload.feed_data(body)
    payload.feed_eof()
    headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
    request = make_mocked_request(method, path, headers=headers, payload=payload, app=app)
    match = await app.router.resolve(request)
    try:
        return await match.handler(request)
    except web.HTTPException as response:
        return response


async def test_health_endpoint_is_live() -> None:
    response = await dispatch(create_app(engine=FakeEngine()), "GET", "/healthz")
    assert response.status == 200
    assert json.loads(response.body) == {"status": "ok"}


async def test_valid_https_url_uses_engine_and_returns_contract() -> None:
    engine = FakeEngine()
    response = await dispatch(
        create_app(engine=engine),
        "POST",
        "/render",
        b'{"url":"https://example.com/page"}',
    )
    assert response.status == 200
    assert json.loads(response.body) == {
        "status": "ok",
        "final_url": "https://example.com/page",
        "title": "Title",
        "text": "Text",
        "truncated": False,
    }
    assert engine.urls == ["https://example.com/page"]


async def test_non_https_is_blocked_before_browser_work() -> None:
    engine = FakeEngine()
    response = await dispatch(
        create_app(engine=engine), "POST", "/render", b'{"url":"http://example.com/"}'
    )
    assert response.status == 200
    assert json.loads(response.body)["status"] == "blocked_url"
    assert engine.urls == []


async def test_wrong_content_type_and_bad_schema_are_blocked() -> None:
    engine = FakeEngine()
    wrong_type = await dispatch(
        create_app(engine=engine),
        "POST",
        "/render",
        b'{"url":"https://example.com"}',
        "text/plain",
    )
    wrong_schema = await dispatch(
        create_app(engine=engine),
        "POST",
        "/render",
        b'{"url":"https://example.com","x":1}',
    )
    assert wrong_type.status == wrong_schema.status == 200
    assert json.loads(wrong_type.body)["status"] == "blocked_url"
    assert json.loads(wrong_schema.body)["status"] == "blocked_url"
    assert engine.urls == []


async def test_oversized_and_malformed_requests_are_400() -> None:
    engine = FakeEngine()
    oversized = await dispatch(create_app(engine=engine), "POST", "/render", b" " * 4097)
    malformed = await dispatch(create_app(engine=engine), "POST", "/render", b"{")
    assert oversized.status == malformed.status == 400
    assert engine.urls == []


async def test_engine_exception_text_never_reaches_response_or_logs(caplog) -> None:
    class LeakyEngine(FakeEngine):
        async def render(self, url):
            self.urls.append(url)
            raise RuntimeError("hostile page text / private address 10.0.0.8")

    response = await dispatch(
        create_app(engine=LeakyEngine()), "POST", "/render", b'{"url":"https://example.com/"}'
    )
    data = response.text
    assert response.status == 200
    assert '"status": "error"' in data
    assert "hostile page text" not in data
    assert "10.0.0.8" not in data
    assert "hostile page text" not in caplog.text


@pytest.mark.parametrize(
    "status", ["ok", "blocked_by_site", "blocked_url", "timeout", "too_large", "busy", "error"]
)
async def test_every_renderer_outcome_uses_http_200(status: str) -> None:
    class StatusEngine(FakeEngine):
        async def render(self, url):
            self.urls.append(url)
            return RenderResult(status)

    response = await dispatch(
        create_app(engine=StatusEngine()), "POST", "/render", b'{"url":"https://example.com/"}'
    )
    assert response.status == 200
    assert json.loads(response.body)["status"] == status


def test_settings_have_no_secrets_and_no_override_fields() -> None:
    settings = RendererSettings()
    fields = type(settings).model_fields
    assert not any(field.annotation is SecretStr for field in fields.values())
    assert "overrides" not in fields
    accepted = {field.alias or name for name, field in fields.items()}
    assert accepted == {
        "RENDER_MAX_CHARS",
        "RENDER_MAX_BYTES",
        "RENDER_CONCURRENCY",
        "RENDER_TIMEOUT_S",
        "RENDER_NETWORK_IDLE_S",
        "RENDER_SETTLE_S",
        "RENDER_MAX_RENDERS_PER_BROWSER",
        "BROWSER_NO_SANDBOX",
        "NET_GUARD_EXTRA_BLOCKED",
        "CARD_MAX_HTML_BYTES",
        "CARD_TIMEOUT_S",
        "CARD_MAX_PAGES",
        "CARD_PAGE_CSS_PX",
        "CARD_MAX_PNG_BYTES",
    }


async def test_card_endpoint_validates_and_returns_base64_pages():
    response = await dispatch(
        create_app(engine=FakeEngine()),
        "POST",
        "/card",
        b'{"html":"<p>ok</p>","theme":"dark","width":400}',
    )
    assert response.status == 200
    assert json.loads(response.body)["status"] == "ok"


async def test_card_endpoint_rejects_extra_keys():
    response = await dispatch(
        create_app(engine=FakeEngine()),
        "POST",
        "/card",
        b'{"html":"x","theme":"light","width":400,"extra":1}',
    )
    assert json.loads(response.body) == {"status": "invalid"}


async def test_card_engine_exception_maps_to_error_and_logs_fixed_status(caplog):
    class BrokenEngine(FakeEngine):
        async def render_card(self, _html, _theme):
            raise RuntimeError("private page text")

    with caplog.at_level("INFO", logger="chatcore_browser_renderer"):
        response = await dispatch(
            create_app(engine=BrokenEngine()),
            "POST",
            "/card",
            b'{"html":"<p>ok</p>","theme":"light","width":400}',
        )
    assert json.loads(response.body) == {"status": "error"}
    assert "card status=error" in caplog.text
    assert "private page text" not in caplog.text


def test_card_page_css_pixel_cap_is_telegram_safe():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RendererSettings(CARD_PAGE_CSS_PX=3601)


def test_service_settings_ignore_hostile_override_environment(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("NET_GUARD_ALLOW_PRIVATE", "1")
    monkeypatch.setenv("BROWSER_IGNORE_CERT_ERRORS", "1")
    settings = RendererSettings()
    assert "overrides" not in type(settings).model_fields
    assert not any("ALLOW_PRIVATE" in field for field in type(settings).model_fields)
