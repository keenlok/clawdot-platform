"""Small, bounded HTTP client for the isolated renderer service."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from chatcore.security.net_guard import failure_cause

log = logging.getLogger(__name__)

STATUS_MESSAGES = {
    "blocked_by_site": (
        "This site blocked automated access, so I can't read it. Please open the link yourself."
    ),
    "blocked_url": "That address is not allowed.",
    "timeout": "The page took too long to render.",
    "too_large": "The page was too large to read.",
    "busy": "The page reader is busy. Try again in a minute.",
}
GENERIC_ERROR = "The page could not be rendered."
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True, slots=True)
class RenderedPage:
    status: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    truncated: bool = False


class RenderClient:
    def __init__(
        self,
        base_url: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        try:
            endpoint = urlsplit(base_url)
        except ValueError as exc:
            raise ValueError("renderer endpoint is invalid") from exc
        if endpoint.username is not None or endpoint.password is not None:
            raise ValueError("renderer endpoint must not contain userinfo")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.transport = transport

    async def render(self, url: str, *, max_response_bytes: int) -> RenderedPage:
        host = urlsplit(url).hostname or ""
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_s,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/render", json={"url": url}
                ) as response:
                    if response.status_code in {403, 429}:
                        return RenderedPage("blocked_by_site")
                    if not 200 <= response.status_code < 300:
                        return RenderedPage("error")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65_536):
                        if len(chunks) + len(chunk) > max_response_bytes:
                            log.info(
                                "renderer response exceeded cap host=%s bytes=%d", host, len(chunks)
                            )
                            return RenderedPage("error")
                        chunks.extend(chunk)
            payload = json.loads(chunks)
            if not isinstance(payload, dict):
                return RenderedPage("error")
            status = payload.get("status")
            if status != "ok":
                if status in STATUS_MESSAGES:
                    return RenderedPage(status)
                return RenderedPage("error")
            final_url = payload.get("final_url")
            title = payload.get("title")
            text = payload.get("text")
            truncated = payload.get("truncated")
            if (
                not isinstance(final_url, str)
                or not _valid_final_url(final_url)
                or not isinstance(title, str)
                or not isinstance(text, str)
                or type(truncated) is not bool
            ):
                return RenderedPage("error")
            return RenderedPage(
                "ok",
                _CONTROL.sub("", final_url)[:2048],
                _CONTROL.sub("", title)[:500],
                _CONTROL.sub("", text)[:1_000_000],
                truncated,
            )
        except (httpx.HTTPError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log.warning(
                "renderer request failed host=%s error=%s cause=%s",
                host,
                type(exc).__name__,
                failure_cause(exc),
            )
            return RenderedPage("error")


def _valid_final_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() != "https" or not parts.hostname:
            return False
        if parts.username is not None or parts.password is not None:
            return False
        host = parts.hostname.rstrip(".")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                socket.inet_aton(host)
                return False
            except OSError:
                pass
            ascii_host = host.encode("idna").decode("ascii").lower()
            labels = ascii_host.split(".")
            if all(re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]+", label) for label in labels):
                return False
            if not any("a" <= character <= "z" for character in labels[-1]):
                return False
            return bool(
                labels
                and all(
                    label
                    and len(label) <= 63
                    and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                    for label in labels
                )
            )
        return address.is_global
    except ValueError:
        return False
