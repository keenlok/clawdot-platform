"""Render model Markdown as text and native Telegram entities, never executable HTML."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError
from telegram import MessageEntity

_PARSER = MarkdownIt("commonmark", {"html": False}).enable(["strikethrough", "table"])
_INLINE_STYLES = {"strong": "bold", "em": "italic", "s": "strikethrough", "link": "text_link"}
_HTTP_URL = TypeAdapter(AnyHttpUrl)


def _link_url(value: str | None) -> str | None:
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ("http", "https"):
            if not parsed.hostname:
                return None
            _HTTP_URL.validate_python(value)
            return value
        if parsed.scheme == "tg" and parsed.hostname:
            return value
        if parsed.scheme == "mailto" and "@" in parsed.path:
            return value
    except (ValueError, ValidationError):
        return None
    return None


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class FormattedText:
    text: str
    entities: tuple[MessageEntity, ...] = ()


@dataclass
class _Writer:
    parts: list[str] = field(default_factory=list)
    entities: list[MessageEntity] = field(default_factory=list)
    marks: list[tuple[str | None, int, str | None]] = field(default_factory=list)
    length: int = 0
    trailing_newlines: int = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self.parts.append(text)
        self.length += utf16_length(text)
        tail = len(text) - len(text.rstrip("\n"))
        self.trailing_newlines = self.trailing_newlines + tail if tail == len(text) else tail

    def newline(self, count: int = 1) -> None:
        if self.length:
            self.append("\n" * max(0, count - self.trailing_newlines))

    def open(self, kind: str | None, url: str | None = None) -> None:
        self.marks.append((kind, self.length, url))

    def close(self) -> None:
        kind, start, url = self.marks.pop()
        if kind and self.length > start:
            self.entities.append(MessageEntity(kind, start, self.length - start, url=url))

    def code(self, text: str, language: str | None = None, *, block: bool = False) -> None:
        start = self.length
        self.append(text)
        # A text link cannot contain a code entity. Retain the clickable label in that case.
        if self.length > start and not any(m[0] == "text_link" for m in self.marks):
            self.entities.append(
                MessageEntity(
                    "pre" if block else "code", start, self.length - start, language=language
                )
            )


def _inline(writer: _Writer, tokens: list[Token], quote_depth: int) -> None:
    for token in tokens:
        stem, _, ending = token.type.rpartition("_")
        if stem in _INLINE_STYLES and ending == "open":
            url = _link_url(token.attrGet("href")) if stem == "link" else None
            kind = _INLINE_STYLES[stem]
            if stem == "link" and (url is None or any(m[0] == "text_link" for m in writer.marks)):
                kind = None
            writer.open(kind, url)
        elif stem in _INLINE_STYLES and ending == "close":
            writer.close()
        elif token.type in ("softbreak", "hardbreak"):
            writer.append("\n" + "› " * quote_depth)
        elif token.type == "code_inline":
            writer.code(token.content)
        elif token.type == "image":
            in_link = any(m[0] == "text_link" for m in writer.marks)
            if not in_link:
                url = _link_url(token.attrGet("src"))
                writer.open("text_link" if url else None, url)
            _inline(writer, token.children or [], quote_depth)
            if not in_link:
                writer.close()
        else:
            writer.append(token.content)


def _entities_without_code_overlap(writer: _Writer, length: int) -> tuple[MessageEntity, ...]:
    code = [(e.offset, e.offset + e.length) for e in writer.entities if e.type in ("code", "pre")]
    result = set()
    for entity in writer.entities:
        ranges = [(entity.offset, min(length, entity.offset + entity.length))]
        if entity.type not in ("code", "pre"):
            for start, end in code:
                ranges = [
                    part
                    for left, right in ranges
                    for part in ((left, min(right, start)), (max(left, end), right))
                    if part[0] < part[1]
                ]
        for start, end in ranges:
            if start < end:
                result.add((start, end - start, entity.type, entity.url, entity.language))
    return tuple(
        MessageEntity(kind, start, size, url=url, language=language)
        for start, size, kind, url, language in sorted(result, key=lambda e: (e[0], -e[1], e[2]))
    )


def render_markdown(source: str) -> FormattedText:
    if not source:
        raise ValueError("message text cannot be empty")
    writer = _Writer()
    lists: list[int | None] = []
    quote_depth = 0
    cell = 0
    for token in _PARSER.parse(source):
        kind = token.type
        if kind == "inline":
            _inline(writer, token.children or [], quote_depth)
        elif kind == "heading_open":
            writer.newline(2)
            writer.open("bold")
        elif kind == "heading_close":
            writer.close()
            writer.newline(2)
        elif kind == "paragraph_open" and quote_depth:
            writer.newline()
            writer.append("› " * quote_depth)
        elif kind == "paragraph_close" and not token.hidden:
            writer.newline(2)
        elif kind in ("bullet_list_open", "ordered_list_open"):
            writer.newline(1 if lists else 2)
            lists.append(int(token.attrGet("start") or 1) if kind == "ordered_list_open" else None)
        elif kind == "list_item_open":
            writer.newline()
            number = lists[-1]
            writer.append("  " * (len(lists) - 1) + (f"{number}. " if number is not None else "• "))
            if number is not None:
                lists[-1] = number + 1
        elif kind == "list_item_close":
            writer.newline()
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
            writer.newline(1 if lists else 2)
        elif kind == "blockquote_open":
            writer.newline(2)
            quote_depth += 1
        elif kind == "blockquote_close":
            quote_depth -= 1
            writer.newline(2)
        elif kind in ("fence", "code_block"):
            writer.newline(2)
            language = token.info.split()[0] if token.info.strip() else None
            writer.code(token.content.rstrip("\n"), language, block=True)
            writer.newline(2)
        elif kind == "hr":
            writer.newline(2)
            writer.append("────────")
            writer.newline(2)
        elif kind in ("table_open", "table_close"):
            writer.newline(2)
        elif kind == "tr_open":
            writer.newline()
            cell = 0
        elif kind in ("td_open", "th_open"):
            if cell:
                writer.append(" | ")
            cell += 1
            if kind == "th_open":
                writer.open("bold")
        elif kind == "th_close":
            writer.close()
        elif kind == "tr_close":
            writer.newline()
    text = "".join(writer.parts).rstrip("\n")
    if not text:
        return FormattedText(source)
    return FormattedText(text, _entities_without_code_overlap(writer, utf16_length(text)))


def split_message(message: FormattedText, limit: int) -> list[FormattedText]:
    """Split without bisecting a UTF-16 character; intersect/rebase native entities per part."""
    offsets = [0]
    for char in message.text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    result = []
    start = 0
    while start < len(message.text):
        end = bisect_right(offsets, offsets[start] + limit) - 1
        if end == start:
            raise ValueError("message limit cannot fit a character")
        if end < len(message.text):
            newline = message.text.rfind("\n", start, end)
            if newline > start:
                end = newline + 1
        left, right = offsets[start], offsets[end]
        entities = []
        for entity in message.entities:
            entity_start, entity_end = (
                max(left, entity.offset),
                min(right, entity.offset + entity.length),
            )
            if entity_start < entity_end:
                entities.append(
                    MessageEntity(
                        entity.type,
                        entity_start - left,
                        entity_end - entity_start,
                        url=entity.url,
                        language=entity.language,
                    )
                )
        result.append(FormattedText(message.text[start:end], tuple(entities)))
        start = end
    return result
