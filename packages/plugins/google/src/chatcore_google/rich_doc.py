"""Bounded model-facing Google Docs block compiler and read-back verifier."""

from __future__ import annotations

import asyncio
import json
import time
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

from chatcore_google.edit_snapshot import utf16_length

_BLOCK_TYPES = {"title", "heading", "paragraph", "bullets", "numbered", "table", "divider"}
_MARKS = {"bold", "italic", "underline", "code", "link"}


@dataclass(frozen=True)
class TableSpec:
    placeholder_index: int
    header: tuple[str, ...]
    rows: tuple[tuple[str | None, ...], ...]


@dataclass(frozen=True)
class DocPlan:
    body_text: str
    batch_a: tuple[dict, ...]
    batch_b: tuple[dict, ...]
    tables: tuple[TableSpec, ...]
    expected: tuple[tuple, ...]
    bullet_texts: tuple[str, ...]
    numbered_texts: tuple[str, ...]
    expected_marks: tuple[tuple[int, int, int, dict], ...]


def _fail(where, message):
    raise ValueError(f"{where}: {message}")


def _keys(value, allowed, where):
    if not isinstance(value, dict):
        _fail(where, "must be an object.")
    unknown = set(value) - allowed
    if unknown:
        _fail(where, f"unknown field {sorted(unknown)[0]!r}.")


def _text(value, where, *, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value):
        _fail(where, "must be a nonempty string." if nonempty else "must be a string or null.")
    if any(unicodedata.category(char) == "Cc" or char in "\u2028\u2029" for char in value):
        _fail(where, "must not contain line breaks or control characters.")
    return value


def _span_list(spans, where):
    if not isinstance(spans, list) or not 1 <= len(spans) <= 100:
        _fail(where, "provide 1–100 formatted spans.")
    clean, total = [], 0
    for index, span in enumerate(spans, 1):
        location = f"{where}, span {index}"
        _keys(span, {"text", *_MARKS}, location)
        value = _text(span.get("text"), location, nonempty=True)
        item = {"text": value}
        total += len(value)
        for mark in _MARKS - {"link"}:
            if mark in span:
                if type(span[mark]) is not bool:
                    _fail(location, f"{mark} must be true or false.")
                if span[mark]:
                    item[mark] = True
        if "link" in span:
            link = span["link"]
            try:
                parsed = urlsplit(link) if isinstance(link, str) else None
            except ValueError:
                parsed = None
            if (
                not isinstance(link, str)
                or len(link) > 2000
                or any(char.isspace() or unicodedata.category(char) == "Cc" for char in link)
                or parsed is None
                or parsed.scheme.casefold() not in {"http", "https"}
                or not parsed.netloc
                or not _hostname(parsed)
            ):
                _fail(location, "link must be an http or https URL with a host.")
            item["link"] = link
        clean.append(item)
    return tuple(clean), total


def _hostname(parsed):
    try:
        return parsed.hostname
    except ValueError:
        return None


def _plain_or_spans(block, where):
    has_text, has_spans = "text" in block, "spans" in block
    if has_text == has_spans:
        _fail(where, "provide exactly one of text or spans.")
    if has_text:
        value = _text(block["text"], where, nonempty=True)
        return (({"text": value},), len(value))
    return _span_list(block["spans"], where)


def compile_doc(blocks, settings) -> DocPlan:
    """Validate rich Doc blocks and compile all requests without performing I/O."""
    try:
        encoded_length = len(json.dumps(blocks, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        raise ValueError("Rich Doc blocks must be JSON-compatible.") from None
    if encoded_length > settings.output_max_chars:
        raise ValueError(
            f"Rich Doc input exceeds GOOGLE_OUTPUT_MAX_CHARS ({settings.output_max_chars})."
        )
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 200:
        raise ValueError("Provide 1–200 rich Doc blocks.")

    lines: list[str] = []
    styles: list[tuple[int, int, str]] = []
    text_marks: list[tuple[int, int, dict]] = []
    marks_for_verification: list[tuple[int, int, int, dict]] = []
    dividers: list[tuple[int, int]] = []
    list_ranges: list[tuple[int, int, str]] = []
    table_specs: list[TableSpec] = []
    expected: list[tuple] = []
    paragraph_ordinal = 0
    bullets, numbered = [], []
    total_text = 0
    tables_count = 0

    def add_line(value, named_style="NORMAL_TEXT", spans=None, list_kind=None, divider=False):
        nonlocal total_text, paragraph_ordinal
        start = 1 + utf16_length("\n".join(lines)) + (1 if lines else 0)
        lines.append(value)
        end = start + utf16_length(value) + 1
        styles.append((start, end, named_style))
        expected.append(("paragraph", value, named_style, divider, list_kind))
        current_ordinal = paragraph_ordinal
        paragraph_ordinal += 1
        if spans:
            cursor = start
            for span in spans:
                size = utf16_length(span["text"])
                marks = {
                    key: (
                        True
                        if key in {"bold", "italic", "underline"}
                        else {"fontFamily": "Roboto Mono"}
                        if key == "code"
                        else {"url": span[key]}
                        if key == "link"
                        else None
                    )
                    for key in _MARKS
                    if key in span
                }
                if marks:
                    normalized = {
                        {"code": "weightedFontFamily", "link": "link"}.get(key, key): value
                        for key, value in marks.items()
                    }
                    text_marks.append((cursor, cursor + size, normalized))
                    marks_for_verification.append(
                        (current_ordinal, cursor - start, cursor - start + size, normalized)
                    )
                cursor += size
        if list_kind:
            (bullets if list_kind == "bullets" else numbered).append(value)
        if divider:
            dividers.append((start, end))
        total_text += len(value)
        return start

    for block_num, block in enumerate(blocks, 1):
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            _fail(f"Block {block_num}", "must be an object with a type.")
        kind = block["type"]
        where = f"Block {block_num} ({kind})"
        if kind not in _BLOCK_TYPES:
            _fail(where, "type is unsupported.")
        if kind in {"title", "heading", "paragraph"}:
            allowed = {"type", "text", "spans"} | ({"level"} if kind == "heading" else set())
            _keys(block, allowed, where)
            if kind == "heading":
                level = block.get("level")
                if type(level) is not int or level not in {1, 2, 3}:
                    _fail(where, "level must be 1, 2 or 3.")
                named = f"HEADING_{level}"
            else:
                named = "TITLE" if kind == "title" else "NORMAL_TEXT"
            spans, _ = _plain_or_spans(block, where)
            add_line("".join(span["text"] for span in spans), named, spans)
        elif kind in {"bullets", "numbered"}:
            _keys(block, {"type", "items"}, where)
            items = block.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= 100:
                _fail(where, "provide 1–100 items.")
            range_start = range_end = None
            for item_num, item in enumerate(items, 1):
                item_where = f"{where}, item {item_num}"
                if isinstance(item, str):
                    value = _text(item, item_where, nonempty=True)
                    spans = ({"text": value},)
                else:
                    spans, _ = _span_list(item, item_where)
                    value = "".join(span["text"] for span in spans)
                item_start = add_line(value, spans=spans, list_kind=kind)
                if range_start is None:
                    range_start = item_start
                range_end = item_start + utf16_length(value) + 1
            list_ranges.append((range_start, range_end, kind))
        elif kind == "table":
            _keys(block, {"type", "header", "rows"}, where)
            tables_count += 1
            if tables_count > 10:
                _fail(where, "at most 10 tables are allowed.")
            if "rows" not in block:
                _fail(where, "rows is required.")
            header, rows = block.get("header"), block.get("rows")
            if not isinstance(header, list) or not 1 <= len(header) <= 8:
                _fail(where, "header must contain 1–8 columns.")
            clean_header = tuple(
                _text(cell, f'{where}, column "{cell}"', nonempty=True) for cell in header
            )
            if not isinstance(rows, list) or len(rows) > 50:
                _fail(where, "rows must contain at most 50 rows.")
            clean_rows = []
            for row_num, row in enumerate(rows, 1):
                if not isinstance(row, list) or len(row) != len(header):
                    _fail(f"{where}, row {row_num}", "must have exactly one cell per header.")
                clean_row = []
                for cell, col_name in zip(row, clean_header, strict=True):
                    location = f'{where}, row {row_num}, column "{col_name}"'
                    if cell is not None:
                        cell = _text(cell, location)
                        if len(cell) > settings.max_cell_chars:
                            _fail(location, f"cell exceeds {settings.max_cell_chars} characters.")
                        total_text += len(cell)
                    clean_row.append(cell)
                clean_rows.append(tuple(clean_row))
            for col_name, cell in zip(clean_header, clean_header, strict=True):
                if len(cell) > settings.max_cell_chars:
                    _fail(
                        f'{where}, column "{col_name}"', "header exceeds the cell character limit."
                    )
                total_text += len(cell)
            start = add_line("")
            spec = TableSpec(start, clean_header, tuple(clean_rows))
            table_specs.append(spec)
            expected.pop()
            expected.append(("table", [list(clean_header), *[list(row) for row in clean_rows]]))
        else:
            _keys(block, {"type"}, where)
            add_line("", divider=True)
    if total_text > settings.max_chars:
        raise ValueError(f"Rich Doc text exceeds GOOGLE_MAX_CHARS ({settings.max_chars}).")

    requests = []
    body_text = "\n".join(lines)
    if body_text:
        requests.append({"insertText": {"location": {"index": 1}, "text": body_text}})
    requests.extend(
        {
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {"namedStyleType": name},
                "fields": "namedStyleType",
            }
        }
        for start, end, name in styles
    )
    requests.extend(
        {
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {
                    "borderBottom": {
                        "color": {"color": {"rgbColor": {"red": 0.6, "green": 0.6, "blue": 0.6}}},
                        "width": {"magnitude": 1, "unit": "PT"},
                        "padding": {"magnitude": 1, "unit": "PT"},
                        "dashStyle": "SOLID",
                    }
                },
                "fields": "borderBottom",
            }
        }
        for start, end in dividers
    )
    for start, end, marks in text_marks:
        style, fields = {}, []
        for key, value in marks.items():
            target = {"code": "weightedFontFamily", "link": "link"}.get(key, key)
            style[target] = value
            fields.append(target)
        requests.append(
            {
                "updateTextStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "textStyle": style,
                    "fields": ",".join(fields),
                }
            }
        )
    for start, end, kind in list_ranges:
        preset = (
            "BULLET_DISC_CIRCLE_SQUARE" if kind == "bullets" else "NUMBERED_DECIMAL_ALPHA_ROMAN"
        )
        requests.append(
            {
                "createParagraphBullets": {
                    "range": {"startIndex": start, "endIndex": end},
                    "bulletPreset": preset,
                }
            }
        )
    inserts = []
    for spec in table_specs:
        inserts.append(
            {
                "insertTable": {
                    "rows": 1 + len(spec.rows),
                    "columns": len(spec.header),
                    "location": {"index": spec.placeholder_index},
                }
            }
        )
    inserts.reverse()
    return DocPlan(
        body_text,
        tuple(requests),
        tuple(inserts),
        tuple(table_specs),
        tuple(expected),
        tuple(bullets),
        tuple(numbered),
        tuple(marks_for_verification),
    )


def preview_doc(blocks) -> str:
    """Render the validated visible text for the preview."""
    output = []
    for block in blocks:
        kind = block["type"]
        if kind in {"title", "heading", "paragraph"}:
            output.append(block.get("text", "".join(s["text"] for s in block.get("spans", []))))
        elif kind in {"bullets", "numbered"}:
            for item in block["items"]:
                output.append(item if isinstance(item, str) else "".join(s["text"] for s in item))
        elif kind == "table":
            output.append(" | ".join(block["header"]))
            output.extend(" | ".join(cell or "" for cell in row) for row in block.get("rows", []))
        else:
            output.append("")
    return "\n".join(output)


def _body_tables(document):
    body = _body_content(document)
    if body is None:
        raise ValueError("Google returned unexpected document content.")
    return [item["table"] for item in body if isinstance(item, dict) and "table" in item]


_EXTRA_TAB_PARTS = (
    "positionedObjects",
    "inlineObjects",
    "footnotes",
    "headers",
    "footers",
)


def _body_content(document):
    """Return the only tab's body content, or None if the document has anything else.

    A new rich Doc has exactly one tab with no child tabs, and no headers, footers, footnotes
    or inline/positioned objects. Anything else is content this tool did not write.
    """
    if not isinstance(document, dict):
        return None
    tabs = document.get("tabs")
    if tabs is not None:
        if not isinstance(tabs, list) or len(tabs) != 1 or not isinstance(tabs[0], dict):
            return None
        if tabs[0].get("childTabs"):
            return None
        holder = tabs[0].get("documentTab")
    else:
        holder = document
    if not isinstance(holder, dict) or any(holder.get(part) for part in _EXTRA_TAB_PARTS):
        return None
    if tabs is not None and any(document.get(part) for part in _EXTRA_TAB_PARTS):
        return None
    body = holder.get("body")
    content = body.get("content") if isinstance(body, dict) else None
    return content if isinstance(content, list) else None


async def fill_tables(client, file_id, plan):
    """Read inserted table cell indices and fill from the final cell backwards."""
    if not plan.tables:
        return
    document = await client.document_for_edit(file_id)
    revision = document.get("revisionId")
    actual = _body_tables(document)
    if len(actual) != len(plan.tables):
        raise ValueError("Google returned an unexpected number of tables.")
    requests = []
    for table, spec in zip(actual, plan.tables, strict=True):
        rows = table.get("tableRows", [])
        expected_rows = [spec.header, *spec.rows]
        if len(rows) != len(expected_rows):
            raise ValueError("Google returned an unexpected table shape.")
        for row, expected_row in zip(rows, expected_rows, strict=True):
            cells = row.get("tableCells", [])
            if len(cells) != len(expected_row):
                raise ValueError("Google returned an unexpected table shape.")
            for cell, value in zip(cells, expected_row, strict=True):
                if value is None or value == "":
                    continue
                paragraph = next(
                    (x.get("paragraph") for x in cell.get("content", []) if x.get("paragraph")),
                    None,
                )
                if paragraph is None:
                    raise ValueError("Google returned a table cell without a paragraph.")
                start = paragraph.get("elements", [{}])[0].get("startIndex")
                if start is None:
                    raise ValueError("Google returned a table cell without a stable index.")
                end = int(start) + utf16_length(value)
                requests.append(
                    (int(start), {"insertText": {"location": {"index": int(start)}, "text": value}})
                )
                if row is rows[0]:
                    requests.append(
                        (
                            int(start),
                            {
                                "updateTextStyle": {
                                    "range": {"startIndex": int(start), "endIndex": end},
                                    "textStyle": {"bold": True},
                                    "fields": "bold",
                                }
                            },
                        )
                    )
    requests.sort(key=lambda item: item[0], reverse=True)
    await client.edit_document(file_id, [request for _, request in requests], revision)


async def verify_rich_doc_readback(client, file_id, plan, *, attempts=3):
    """Retry read-back only; a failed content mutation is never repeated."""
    deadline = time.monotonic() + 5.0
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, [], "readback_deadline"
        try:
            document = await asyncio.wait_for(client.document_for_edit(file_id), timeout=remaining)
            verified, warnings = verify_rich_doc(document, plan)
            if verified:
                return True, warnings, None
            category = "text_mismatch"
        except TimeoutError:
            return False, [], "readback_deadline"
        except Exception:
            category = "read_error"
        if attempt + 1 < attempts:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, [], "readback_deadline"
            await asyncio.sleep(min((0.1, 0.4)[attempt], remaining))
    return False, [], category


_SUGGESTION_FIELDS = {
    "suggestedInsertionIds",
    "suggestedDeletionIds",
    "suggestedTextStyleChanges",
    "suggestedParagraphStyleChanges",
    "suggestedBulletChanges",
    "suggestedTableCellStyleChanges",
    "suggestedTableRowStyleChanges",
    "suggestedNamedStylesChanges",
    "suggestedDocumentStyleChanges",
}


_SUGGESTION_FIELDS = frozenset(
    {
        "suggestedInsertionIds",
        "suggestedDeletionIds",
        "suggestedTextStyleChanges",
        "suggestedParagraphStyleChanges",
        "suggestedBulletChanges",
        "suggestedTableCellStyleChanges",
        "suggestedTableRowStyleChanges",
        "suggestedNamedStylesChanges",
        "suggestedDocumentStyleChanges",
        "suggestedPositionedObjectIds",
        "suggestedInlineObjectPropertiesChanges",
    }
)
_MARK_CATEGORY = {
    "bold": "bold/italic/underline",
    "italic": "bold/italic/underline",
    "underline": "bold/italic/underline",
    "weightedFontFamily": "code font",
    "link": "links",
}


def _has_suggestion(value):
    """True only for real pending-suggestion fields, never ``suggestionsViewMode``."""
    if isinstance(value, dict):
        return any(
            (key in _SUGGESTION_FIELDS and child) or _has_suggestion(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_has_suggestion(child) for child in value)
    return False


def _paragraph_text(paragraph):
    """Return the paragraph text without its newline, or None if it holds non-text content."""
    if not isinstance(paragraph, dict) or paragraph.get("positionedObjectIds"):
        return None
    elements = paragraph.get("elements")
    if not isinstance(elements, list):
        return None
    parts = []
    for element in elements:
        if not isinstance(element, dict) or set(element) - {"startIndex", "endIndex", "textRun"}:
            return None
        run = element.get("textRun")
        if not isinstance(run, dict) or not isinstance(run.get("content"), str):
            return None
        parts.append(run["content"])
    text = "".join(parts)
    if not text.endswith("\n") or "\n" in text[:-1]:
        return None
    return text[:-1]


def _style(paragraph):
    style = paragraph.get("paragraphStyle")
    return style if isinstance(style, dict) else {}


def _is_padding(node):
    if node[0] != "paragraph" or node[1] != "":
        return False
    paragraph = node[2]
    style = _style(paragraph)
    return (
        style.get("namedStyleType", "NORMAL_TEXT") == "NORMAL_TEXT"
        and not any(
            value and (key.startswith("border") or key == "shading") for key, value in style.items()
        )
        and not paragraph.get("bullet")
    )


def _table_matches(table, wanted):
    rows = table.get("tableRows")
    if not isinstance(rows, list) or len(rows) != len(wanted):
        return False
    for row, wanted_row in zip(rows, wanted, strict=True):
        cells = row.get("tableCells") if isinstance(row, dict) else None
        if not isinstance(cells, list) or len(cells) != len(wanted_row):
            return False
        for cell, value in zip(cells, wanted_row, strict=True):
            content = cell.get("content") if isinstance(cell, dict) else None
            if (
                not isinstance(content, list)
                or len(content) != 1
                or not isinstance(content[0], dict)
                or set(content[0]) - {"startIndex", "endIndex", "paragraph"}
                or "paragraph" not in content[0]
            ):
                return False
            if _paragraph_text(content[0]["paragraph"]) != (value or ""):
                return False
    return True


def _style_has(actual, expected):
    for key, value in expected.items():
        if isinstance(value, dict):
            got = actual.get(key)
            if not isinstance(got, dict) or any(got.get(k) != v for k, v in value.items()):
                return False
        elif actual.get(key) != value:
            return False
    return True


def verify_rich_doc(document, plan):
    """Compare the read-back body with the frozen plan.

    Returns ``(text_ok, warnings)``. ``text_ok`` is False for any text, structure or table
    difference, unsupported content or pending suggestion. Docs adds one empty unstyled
    paragraph before and after an inserted table; only those are tolerated. Formatting
    differences (named styles, bullets, divider border, span marks) are warnings.
    """
    if _has_suggestion(document):
        return False, []
    content = _body_content(document)
    if content is None:
        return False, []
    nodes = []
    for item in content:
        if not isinstance(item, dict):
            return False, []
        if "sectionBreak" in item:
            continue
        if "paragraph" in item:
            text = _paragraph_text(item["paragraph"])
            if text is None:
                return False, []
            nodes.append(("paragraph", text, item["paragraph"]))
        elif "table" in item:
            nodes.append(("table", None, item["table"]))
        else:
            return False, []
    warnings = set()
    matched = {}
    position = 0
    expected = plan.expected
    for ordinal, wanted in enumerate(expected):
        if wanted[0] == "table":
            if (
                position + 1 < len(nodes)
                and _is_padding(nodes[position])
                and nodes[position + 1][0] == "table"
            ):
                position += 1
            if position >= len(nodes) or nodes[position][0] != "table":
                return False, []
            if not _table_matches(nodes[position][2], wanted[1]):
                return False, []
            position += 1
            if position < len(nodes) and _is_padding(nodes[position]):
                following = expected[ordinal + 1] if ordinal + 1 < len(expected) else None
                next_is_blank = (
                    following is not None and following[0] == "paragraph" and (following[1] == "")
                )
                second_blank = (
                    position + 1 < len(nodes)
                    and nodes[position + 1][0] == "paragraph"
                    and nodes[position + 1][1] == ""
                )
                if not next_is_blank or second_blank:
                    position += 1
            continue
        if position >= len(nodes) or nodes[position][0] != "paragraph":
            return False, []
        _, text, paragraph = nodes[position]
        if text != wanted[1]:
            return False, []
        style = _style(paragraph)
        if style.get("namedStyleType", "NORMAL_TEXT") != wanted[2]:
            warnings.add("heading style")
        if len(wanted) > 3 and wanted[3] and not style.get("borderBottom"):
            warnings.add("divider border")
        if len(wanted) > 4 and bool(wanted[4]) != bool(paragraph.get("bullet")):
            warnings.add("list bullets")
        matched[ordinal] = paragraph
        position += 1
    if position != len(nodes):
        return False, []
    for ordinal, start, end, expected_style in plan.expected_marks:
        paragraph = matched.get(ordinal)
        elements = paragraph.get("elements", []) if paragraph else []
        base = elements[0].get("startIndex") if elements else None
        categories = {_MARK_CATEGORY.get(key, "formatting") for key in expected_style}
        if not isinstance(base, int):
            warnings.update(categories)
            continue
        runs = []
        for element in elements:
            left, right = element.get("startIndex"), element.get("endIndex")
            if isinstance(left, int) and isinstance(right, int):
                if left < base + end and right > base + start:
                    runs.append(element["textRun"].get("textStyle") or {})
        for key, value in expected_style.items():
            if not runs or not all(_style_has(run, {key: value}) for run in runs):
                warnings.add(_MARK_CATEGORY.get(key, "formatting"))
    return True, sorted(warnings)
