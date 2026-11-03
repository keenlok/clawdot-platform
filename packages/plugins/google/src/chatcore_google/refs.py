"""Strict Google link parsing and human-origin file authority."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_DOC = re.compile(r"^/(document|spreadsheets|presentation)/(?:u/\d+/)?d/([A-Za-z0-9_-]+)(?:/.*)?$")
_FILE = re.compile(r"^/file/d/([A-Za-z0-9_-]+)(?:/.*)?$")


@dataclass(frozen=True)
class GoogleRef:
    kind: str
    file_id: str
    gid: int | None = None
    doc_tab: str | None = None


def parse_google_ref(url: str) -> GoogleRef:
    try:
        parts = urlsplit(url.strip())
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or parts.hostname not in {"docs.google.com", "drive.google.com"}
        ):
            raise ValueError
        query = parse_qs(parts.query)
        fragment = parse_qs(parts.fragment)
        kind = "drive_file"
        if parts.hostname == "docs.google.com":
            match = _DOC.fullmatch(parts.path)
            if not match:
                raise ValueError
            kind, file_id = match.groups()
        else:
            match = _FILE.fullmatch(parts.path)
            if match:
                file_id = match.group(1)
            elif parts.path == "/open" and len(query.get("id", [])) == 1:
                file_id = query["id"][0]
            else:
                raise ValueError
        if not _ID.fullmatch(file_id):
            raise ValueError
        gids = query.get("gid", []) + fragment.get("gid", [])
        if len(gids) > 1 or (gids and (not gids[0].isascii() or not gids[0].isdigit())):
            raise ValueError
        tab = query.get("tab", [])
        if len(tab) > 1 or (tab and not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", tab[0])):
            raise ValueError
        return GoogleRef(kind, file_id, int(gids[0]) if gids else None, tab[0] if tab else None)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Provide a valid HTTPS Google Docs, Sheets or Drive file link.") from None


def approved_ref(url: str, human_urls: frozenset[str]) -> GoogleRef:
    requested = parse_google_ref(url)
    for human_url in human_urls:
        try:
            if parse_google_ref(human_url).file_id == requested.file_id:
                return requested
        except ValueError:
            continue
    raise ValueError("Post the file link in a human message or its reply chain first.")


async def approved_source_ref(url, ctx, source_message_ref) -> GoogleRef:
    """Verify a retained human post directly; excerpts and bot answers grant no authority."""
    if (
        not isinstance(source_message_ref, str)
        or not 1 <= len(source_message_ref) <= 128
        or any(ord(char) < 32 for char in source_message_ref)
    ):
        raise ValueError("Provide the original human message ID as source_message_ref.")
    source = await ctx.stores.repo.get_message(ctx.chat, source_message_ref)
    if (
        source is None
        or source.message_id != source_message_ref
        or source.chat != ctx.chat
        or source.thread_id != ctx.thread_id
        or source.is_bot
    ):
        raise ValueError("The source must be an existing human message in this chat and topic.")
    urls = frozenset(
        candidate.rstrip(".,;:!?)]}")
        for candidate in re.findall(r"https?://[^\s<>]+", source.text, re.IGNORECASE)
    )
    try:
        return approved_ref(url, urls)
    except ValueError:
        raise ValueError("The referenced human message does not contain that file link.") from None
