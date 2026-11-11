"""Strict data model for fixed-template summary cards."""

from __future__ import annotations

import unicodedata
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import CardError

Tone = Literal["info", "good", "warning", "critical"]
_BIDI = dict.fromkeys((*range(0x202A, 0x202F), *range(0x2066, 0x206A)), None)


def _clean(value: str, *, newline: bool = False) -> str:
    if not isinstance(value, str):
        return value
    for char in value:
        if unicodedata.category(char) == "Cc" and not (newline and char == "\n"):
            raise ValueError("control characters are not allowed")
    return value.translate(_BIDI).strip()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")


class StatTile(StrictModel):
    label: str = Field(min_length=1, max_length=24)
    value: str = Field(min_length=1, max_length=18)
    note: str | None = Field(default=None, max_length=48)
    tone: Tone | None = None

    @field_validator("label", "value", "note", mode="before")
    @classmethod
    def clean_text(cls, value):
        return _clean(value) if value is not None else value


class StatsBlock(StrictModel):
    type: Literal["stats"]
    tiles: list[StatTile] = Field(min_length=1, max_length=4)


class HeadingBlock(StrictModel):
    type: Literal["heading"]
    text: str = Field(min_length=1, max_length=80)

    @field_validator("text", mode="before")
    @classmethod
    def clean_text(cls, value):
        return _clean(value)


class ParagraphBlock(StrictModel):
    type: Literal["paragraph"]
    text: str = Field(min_length=1, max_length=1000)

    @field_validator("text", mode="before")
    @classmethod
    def clean_text(cls, value):
        return _clean(value, newline=True)


class BulletsBlock(StrictModel):
    type: Literal["bullets"]
    items: list[str] = Field(min_length=1, max_length=12)

    @field_validator("items", mode="before")
    @classmethod
    def clean_items(cls, values):
        if not isinstance(values, list):
            raise ValueError("items must be a list")
        return [_clean(v) for v in values]

    @model_validator(mode="after")
    def check_item_lengths(self):
        if any(not 1 <= len(item) <= 200 for item in self.items):
            raise ValueError("items must be 1 to 200 characters")
        return self


class TableColumn(StrictModel):
    header: str = Field(min_length=1, max_length=24)
    align: Literal["start", "end"] = "start"

    @field_validator("header", mode="before")
    @classmethod
    def clean_text(cls, value):
        return _clean(value)


class TableBlock(StrictModel):
    type: Literal["table"]
    columns: list[TableColumn] = Field(min_length=1, max_length=5)
    rows: list[list[str]] = Field(min_length=1, max_length=40)
    total: list[str] | None = None

    @field_validator("rows", mode="before")
    @classmethod
    def clean_rows(cls, rows):
        if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
            raise ValueError("rows must be a list of rows")
        if any(not isinstance(value, str) for row in rows for value in row):
            raise ValueError("cells must be strings")
        cleaned = [[_clean(v) for v in row] for row in rows]
        if any(any(len(v) > 60 for v in row) for row in cleaned):
            raise ValueError("cells must be at most 60 characters")
        return cleaned

    @field_validator("total", mode="before")
    @classmethod
    def clean_total(cls, total):
        if total is None:
            return total
        if not isinstance(total, list):
            raise ValueError("total must be a row")
        if any(not isinstance(value, str) for value in total):
            raise ValueError("total cells must be strings")
        cleaned = [_clean(value) for value in total]
        if any(len(value) > 60 for value in cleaned):
            raise ValueError("cells must be at most 60 characters")
        return cleaned

    @model_validator(mode="after")
    def check_dimensions(self):
        count = len(self.columns)
        if (
            any(len(row) != count for row in self.rows)
            or self.total is not None
            and len(self.total) != count
        ):
            raise ValueError("row length must match columns")
        return self


class CalloutBlock(StrictModel):
    type: Literal["callout"]
    tone: Tone
    text: str = Field(min_length=1, max_length=300)

    @field_validator("text", mode="before")
    @classmethod
    def clean_text(cls, value):
        return _clean(value)


class Source(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=300)

    @field_validator("title", mode="before")
    @classmethod
    def clean_title(cls, value):
        return _clean(value)

    @field_validator("url", mode="before")
    @classmethod
    def valid_url(cls, value):
        if not isinstance(value, str):
            raise ValueError("url must be a string")
        value = _clean(value)
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("url must be http(s) with a hostname and no userinfo")
        return value

    @property
    def hostname(self) -> str:
        host = urlsplit(self.url).hostname or ""
        try:
            if host.isascii():
                host = host.encode("ascii").decode("idna")
        except UnicodeError:
            pass
        return host.removeprefix("www.")


class SourcesBlock(StrictModel):
    type: Literal["sources"]
    items: list[Source] = Field(min_length=1, max_length=8)


Block = Annotated[
    StatsBlock
    | HeadingBlock
    | ParagraphBlock
    | BulletsBlock
    | TableBlock
    | CalloutBlock
    | SourcesBlock,
    Field(discriminator="type"),
]


class Card(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    subtitle: str | None = Field(default=None, max_length=120)
    blocks: list[Block] = Field(default_factory=list, max_length=16)

    @field_validator("title", "subtitle", mode="before")
    @classmethod
    def clean_text(cls, value):
        return _clean(value) if value is not None else value


def validate_card(data: object, *, max_text_chars: int = 8000) -> Card:
    try:
        card = Card.model_validate(data)
        text = card.title + (card.subtitle or "")

        def walk(value):
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                return "".join(walk(item) for item in value)
            if isinstance(value, BaseModel):
                return "".join(walk(v) for k, v in value if k != "url")
            return ""

        size = len(text) + sum(len(v) for block in card.blocks for v in [walk(block)])
        if size > max_text_chars:
            raise CardError("invalid", "card: total text too long")
        return card
    except CardError:
        raise
    except ValidationError as exc:
        error = exc.errors()[0] if exc.errors() else {}
        error_type = error.get("type", "")
        location = error.get("loc", ())
        fields = {
            "title",
            "subtitle",
            "blocks",
            "type",
            "tiles",
            "label",
            "value",
            "note",
            "tone",
            "text",
            "items",
            "header",
            "align",
            "columns",
            "rows",
            "total",
            "url",
        }
        path_parts = [part for part in location if isinstance(part, int) or part in fields]
        if error_type == "extra_forbidden" and path_parts:
            path_parts.pop()
        path = ".".join(map(str, path_parts)) or "card"
        if error_type == "extra_forbidden":
            rule = "unknown field"
        elif error_type in {"string_too_long", "too_long"}:
            rule = "too long"
        elif error_type in {"string_too_short", "too_short"}:
            rule = "too short"
        elif error_type.endswith("_type"):
            rule = "wrong type"
        elif error_type == "value_error":
            error_value = error.get("ctx", {}).get("error")
            message = (
                error_value.args[0]
                if isinstance(error_value, ValueError) and error_value.args
                else ""
            )
            rules = {
                "control characters are not allowed": "control character",
                "cells must be at most 60 characters": "too long",
                "items must be 1 to 200 characters": "too long",
                "items must be a list": "wrong type",
                "rows must be a list of rows": "wrong type",
                "cells must be strings": "wrong type",
                "total must be a row": "wrong type",
                "total cells must be strings": "wrong type",
                "row length must match columns": "row length must match columns",
            }
            rule = rules.get(message, "invalid value")
        else:
            rule = "invalid value"
        raise CardError("invalid", f"{path}: {rule}") from None
    except (ValueError, TypeError):
        raise CardError("invalid", "card: invalid value") from None


def card_json_schema() -> dict:
    """Return the fully inlined tool schema for the card document."""

    def string(description, minimum=0, maximum=None, **extra):
        result = {"type": "string", "description": description, "minLength": minimum}
        if maximum is not None:
            result["maxLength"] = maximum
        result.update(extra)
        return result

    def array(description, items, minimum=0, maximum=None):
        result = {"type": "array", "description": description, "items": items, "minItems": minimum}
        if maximum is not None:
            result["maxItems"] = maximum
        return result

    def obj(description, properties, required):
        return {
            "type": "object",
            "description": description,
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def optional(schema, description):
        return {"anyOf": [schema, {"type": "null"}], "description": description}

    def kind(value):
        return {"type": "string", "const": value, "description": "Block type"}

    tone = string("Status tone", 1, 8, enum=["info", "good", "warning", "critical"])
    align = string("Cell alignment", 1, 5, enum=["start", "end"])
    tile = obj(
        "Stat tile",
        {
            "label": string("Short label", 1, 24),
            "value": string("Stat value", 1, 18),
            "note": optional(string("Optional note", 0, 48), "Optional tile note"),
            "tone": optional(tone, "Optional status tone"),
        },
        ["label", "value"],
    )
    column = obj(
        "Table column", {"header": string("Column heading", 1, 24), "align": align}, ["header"]
    )
    source = obj(
        "Source link",
        {
            "title": string("Source title", 1, 100),
            "url": string("HTTP or HTTPS URL", 1, 300, pattern=r"^https?://[^/@]+(?:/|$)"),
        },
        ["title", "url"],
    )
    blocks = [
        obj(
            "Stat tiles",
            {"type": kind("stats"), "tiles": array("Stat tiles", tile, 1, 4)},
            ["type", "tiles"],
        ),
        obj(
            "Section heading",
            {"type": kind("heading"), "text": string("Heading", 1, 80)},
            ["type", "text"],
        ),
        obj(
            "Paragraph",
            {"type": kind("paragraph"), "text": string("Paragraph text", 1, 1000)},
            ["type", "text"],
        ),
        obj(
            "Bulleted list",
            {
                "type": kind("bullets"),
                "items": array("Bullet items", string("Bullet text", 1, 200), 1, 12),
            },
            ["type", "items"],
        ),
        obj(
            "Table",
            {
                "type": kind("table"),
                "columns": array("Table columns", column, 1, 5),
                "rows": array(
                    "Table rows; each row has exactly one cell per column",
                    array("Row cells", string("Cell text", 0, 60), 1, 5),
                    1,
                    40,
                ),
                "total": optional(
                    array(
                        "Optional total row with exactly one cell per column",
                        string("Cell text", 0, 60),
                        1,
                        5,
                    ),
                    "Optional table total row",
                ),
            },
            ["type", "columns", "rows"],
        ),
        obj(
            "Callout",
            {"type": kind("callout"), "tone": tone, "text": string("Callout text", 1, 300)},
            ["type", "tone", "text"],
        ),
        obj(
            "Sources",
            {"type": kind("sources"), "items": array("Source links", source, 1, 8)},
            ["type", "items"],
        ),
    ]
    return obj(
        "Structured card",
        {
            "title": string("Card title", 1, 80),
            "subtitle": optional(string("Optional subtitle", 0, 120), "Optional subtitle"),
            "blocks": array("Content blocks", {"anyOf": blocks}, 0, 16),
        },
        ["title"],
    )
