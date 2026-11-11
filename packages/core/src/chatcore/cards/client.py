"""HTTP client for the internal card renderer."""

from __future__ import annotations

import base64
import binascii
import json
from urllib.parse import urlsplit

import httpx

from .errors import CardError
from .html import render_card_html
from .models import Card

_MAX_RESPONSE = 28 * 1024 * 1024
_PNG = b"\x89PNG\r\n\x1a\n"


class CardRenderClient:
    def __init__(self, base_url: str, *, timeout_s: float = 20, transport=None) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("renderer URL must be http(s) without userinfo")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.transport = transport
        self._client = None

    async def render(
        self, card: Card, *, theme: str = "light", footer: str | None = None
    ) -> list[bytes]:
        payload = {
            "html": render_card_html(card, theme=theme, footer=footer),
            "theme": theme,
            "width": 400,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_s,
                transport=self.transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream("POST", self.base_url + "/card", json=payload) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > _MAX_RESPONSE:
                            raise CardError("too_large")
                    response.raise_for_status()
            try:
                data = json.loads(content)
            except (ValueError, UnicodeDecodeError):
                raise CardError("unavailable") from None
        except httpx.TimeoutException:
            raise CardError("timeout") from None
        except httpx.HTTPStatusError:
            raise CardError("unavailable") from None
        except httpx.HTTPError:
            raise CardError("unavailable") from None
        if not isinstance(data, dict) or not isinstance(data.get("status"), str):
            raise CardError("unavailable")
        status = data["status"]
        if status != "ok":
            code = {
                "busy": "busy",
                "too_large": "too_large",
                "timeout": "timeout",
                "invalid": "unavailable",
                "error": "unavailable",
            }.get(status, "unavailable")
            raise CardError(code)
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages:
            raise CardError("unavailable")
        result = []
        try:
            for encoded in pages:
                page = base64.b64decode(encoded, validate=True)
                if not page.startswith(_PNG):
                    raise ValueError
                result.append(page)
        except (binascii.Error, ValueError, TypeError):
            raise CardError("unavailable") from None
        return result

    async def aclose(self) -> None:
        return None
