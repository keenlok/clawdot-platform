"""Render model Markdown as text and native Telegram entities, never executable HTML."""

from __future__ import annotations

import re
import unicodedata
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
_PRE_TABLE_WIDTH = 36
_NUMBER = re.compile(r"^[-+−]?[$€£¥]?\s?\d[\d,]*(\.\d+)?\s?%?(\s?[A-Z]{3})?$")


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _table_text(rows: list[list[str]]) -> str | None:
    if not rows or not rows[0]:
        return None
    columns = len(rows[0])
    if any(len(row) != columns for row in rows):
        return None
    widths = [max(_display_width(row[index]) for row in rows) for index in range(columns)]
    if max(sum(widths) + 2 * (columns - 1), 0) > _PRE_TABLE_WIDTH:
        return None
    aligns = [
        bool(rows[1:]) and all(not row[i] or _NUMBER.fullmatch(row[i]) for row in rows[1:])
        for i in range(columns)
    ]

    def pad(value: str, width: int, right: bool) -> str:
        gap = width - _display_width(value)
        return " " * gap + value if right else value + " " * gap

    rendered = [
        "  ".join(pad(cell, widths[i], aligns[i]) for i, cell in enumerate(row)) for row in rows
    ]
    rendered.insert(1, "  ".join("─" * width for width in widths))
    return "\n".join(rendered)


def _plain_inline(tokens: list[Token] | None) -> str:
    return "".join(
        "\n"
        if token.type in {"softbreak", "hardbreak"}
        else _plain_inline(token.children)
        if token.type == "image"
        else token.content
        for token in (tokens or [])
    )


def _table_rows(tokens: list[Token], start: int, end: int) -> list[list[str]]:
    rows: list[list[str]] = []
    row: list[str] | None = None
    cell_open = False
    for token in tokens[start + 1 : end]:
        if token.type == "tr_open":
            row = []
        elif token.type in {"td_open", "th_open"}:
            cell_open = True
        elif token.type == "inline" and row is not None and cell_open:
            row.append(_plain_inline(token.children))
        elif token.type in {"td_close", "th_close"}:
            cell_open = False
        elif token.type == "tr_close" and row is not None:
            rows.append(row)
            row = None
    return rows


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
    quote_start: int | None = None
    quote_depth: int = 0

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
            writer.append("\n")
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
    tokens = _PARSER.parse(source)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == "table_open":
            end = next(
                (pos for pos in range(index + 1, len(tokens)) if tokens[pos].type == "table_close"),
                index,
            )
            table = _table_text(_table_rows(tokens, index, end))
            if table is not None:
                writer.newline(2)
                writer.code(table, block=True)
                writer.newline(2)
                index = end + 1
                continue
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
            if quote_depth == 0:
                writer.quote_start = writer.length
            quote_depth += 1
        elif kind == "blockquote_close":
            quote_depth -= 1
            if (
                quote_depth == 0
                and writer.quote_start is not None
                and writer.length > writer.quote_start
            ):
                writer.entities.append(
                    MessageEntity(
                        "blockquote", writer.quote_start, writer.length - writer.quote_start
                    )
                )
                writer.quote_start = None
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
        index += 1
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
