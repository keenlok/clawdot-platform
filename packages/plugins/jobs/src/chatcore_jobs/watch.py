"""Bounded, guarded text snapshots for public website watches."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import trafilatura
from chatcore.security.net_guard import BlockedURL, FetchError, FetchResult, safe_get

from chatcore_jobs.models import Scope
from chatcore_jobs.settings import JobsSettings

ALLOWED_TYPES = ("text/plain", "text/html", "application/xhtml+xml")
_MAX_NOTIFICATION = 1500
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~])")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class Snapshot:
    text: str
    digest: str


class RenderedBlocked(Exception):
    """A site declined automated browser access."""


class RenderedBusy(Exception):
    """The shared renderer has no free slot."""


RenderedFetcher = Callable[[str, JobsSettings], Awaitable[Snapshot]]
RenderedAllowed = Callable[[Scope], Awaitable[bool]]


def normalize_text(value: str, max_chars: int) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = _CONTROL.sub("", value)
    value = " ".join(value.split())
    if not value:
        raise FetchError("The page had no readable text.")
    if len(value) > max_chars:
        raise FetchError("The readable page text exceeded the configured limit.")
    return value


def extract_text(result: FetchResult, settings: JobsSettings) -> Snapshot:
    if result.status < 200 or result.status >= 300:
        raise FetchError("The page returned an unsuccessful status.")
    if result.truncated or len(result.body) > settings.fetch_max_bytes:
        raise FetchError("The page exceeded the response size limit.")
    mime = result.content_type.split(";", 1)[0].strip().lower()
    if mime not in ALLOWED_TYPES:
        raise FetchError("The page returned a disallowed content type.")
    body = result.body.decode("utf-8", errors="replace")
    if mime == "text/plain":
        extracted = body
    else:
        extracted = (
            trafilatura.extract(
                body, include_comments=False, include_tables=False, favor_precision=True
            )
            or ""
        )
    text = normalize_text(extracted, settings.extracted_max_chars)
    return Snapshot(text, hashlib.sha256(text.encode("utf-8")).hexdigest())


async def fetch_snapshot(url: str, settings: JobsSettings) -> Snapshot:
    result = await safe_get(
        url,
        max_bytes=settings.fetch_max_bytes,
        timeout_s=settings.fetch_timeout_seconds,
        allowed_types=ALLOWED_TYPES,
    )
    return extract_text(result, settings)


def _quoted(value: str, limit: int) -> str:
    value = _CONTROL.sub("", value)
    value = value.replace("@", "＠")
    value = _MARKDOWN.sub(r"\\\1", value)
    value = " ".join(value.split())
    return value[:limit]


def change_notification(old: str, new: str) -> str:
    """Return a one-message plain prose diff using a linear prefix/suffix scan."""
    prefix = 0
    for left, right in zip(old, new, strict=False):
        if left != right:
            break
        prefix += 1
    suffix = 0
    old_remaining, new_remaining = len(old) - prefix, len(new) - prefix
    while suffix < old_remaining and suffix < new_remaining:
        if old[len(old) - suffix - 1] != new[len(new) - suffix - 1]:
            break
        suffix += 1
    old_end = len(old) - suffix
    new_end = len(new) - suffix
    removed = _quoted(old[prefix:old_end], 350)
    added = _quoted(new[prefix:new_end], 350)
    snippets = []
    if removed:
        snippets.append("Removed: “" + removed + "”")
    if added:
        snippets.append("Added: “" + added + "”")
    body = "\n".join(snippets) or "The readable text changed."
    return ("Website text changed.\n" + body)[:_MAX_NOTIFICATION]


def safe_error_message(error: BaseException, *, rendered: bool = False) -> str:
    if isinstance(error, BlockedURL):
        return "The watch stopped because its destination is not allowed. Review the public URL."
    if isinstance(error, FetchError):
        if rendered:
            return "The rendered page could not be read. The watch will retry later."
        return (
            "The watch could not read a supported public text page. Check that it is "
            "reachable and returns readable text."
        )
    if rendered:
        return "The rendered page could not be read. The watch will retry later."
    return "The watch encountered a temporary retrieval error. It will retry later."
