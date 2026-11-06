"""Bounded tab-aware Docs snapshots, including ordinary table-cell paragraphs."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from chatcore_google.edit_models import DocMatch, DocParagraph, DocSnapshot


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _occurrences(value: str, target: str) -> int:
    count = start = 0
    while True:
        found = value.find(target, start)
        if found < 0:
            return count
        count += 1
        start = found + 1


def _walk_tabs(document):
    output = []

    def visit(tab):
        props = tab.get("tabProperties") or {}
        output.append((props, tab.get("documentTab") or {}))
        for child in tab.get("childTabs", ()):
            visit(child)

    for tab in document.get("tabs", ()):
        visit(tab)
    if not output:
        output.append(({"tabId": ""}, {"body": document.get("body") or {}}))
    return output


def flatten_doc_snapshot(document, *, tab_id, max_chars, file_id=None):
    """Return selected-tab main-body text with exact server UTF-16 indices."""
    revision = document.get("revisionId")
    if not isinstance(revision, str) or not revision:
        raise ValueError("Google returned a document without a stable revision ID.")
    tabs = _walk_tabs(document)
    if tab_id is None:
        if len(tabs) != 1:
            raise ValueError("Specify an explicit tab because this document has multiple tabs.")
        props, body_tab = tabs[0]
        resolved_tab_id = props.get("tabId")
    else:
        selected = [(props, value) for props, value in tabs if props.get("tabId") == tab_id]
        if len(selected) != 1:
            raise ValueError("The requested document tab was not found uniquely.")
        props, body_tab = selected[0]
        resolved_tab_id = props.get("tabId")
    if not isinstance(resolved_tab_id, str) or not resolved_tab_id:
        raise ValueError("Google did not return a stable tab ID required for edits.")

    paragraphs: list[DocParagraph] = []
    if "namedStyles" in body_tab:
        named_styles = body_tab["namedStyles"]
        named_style_owner = body_tab
    elif not document.get("tabs") or len(tabs) == 1:
        named_styles = document.get("namedStyles", {})
        named_style_owner = document
    else:
        named_styles = {}
        named_style_owner = body_tab
    style_rows = named_styles.get("styles", ()) if isinstance(named_styles, dict) else ()
    style_context_supported = not bool(named_style_owner.get("suggestedNamedStylesChanges"))
    if not isinstance(style_rows, list | tuple) or any(
        not isinstance(row, dict) for row in style_rows
    ):
        style_rows = ()
        style_context_supported = False
    used = 0
    complete = True

    def has_suggestions(value):
        return any(
            value.get(field) or value.get(field + "State")
            for field in (
                "suggestedInsertionIds",
                "suggestedDeletionIds",
                "suggestedTextStyleChanges",
            )
        )

    def collect_content(content, inherited_suggestion=False, in_table=False):
        nonlocal used, complete
        for structural in content or ():
            suggested = inherited_suggestion or has_suggestions(structural)
            if suggested:
                complete = False
            if "paragraph" in structural:
                paragraph = structural["paragraph"]
                runs = []
                text_parts = []
                start = structural.get("startIndex")
                end = structural.get("endIndex")
                if start is None or end is None:
                    raise ValueError("Google returned document content without stable indexes.")
                for element in paragraph.get("elements", ()):
                    text_run = element.get("textRun")
                    if text_run is None:
                        if element.get("startIndex") != element.get("endIndex"):
                            complete = False
                            runs.append({"structural": True, **element})
                        continue
                    content_text = text_run.get("content", "")
                    if not isinstance(content_text, str):
                        raise ValueError("Google returned an invalid document text run.")
                    text_parts.append(content_text)
                    runs.append(
                        {
                            "start_index": element.get("startIndex"),
                            "end_index": element.get("endIndex"),
                            "text": content_text,
                            "style": text_run.get("textStyle") or {},
                            "suggestions": suggested or has_suggestions(text_run),
                        }
                    )
                text = "".join(text_parts)
                used += len(text)
                if used > max_chars:
                    raise ValueError("Document exceeds the edit snapshot size limit.")
                paragraph_style = paragraph.get("paragraphStyle") or {}
                paragraph_style_supported = isinstance(paragraph_style, dict) and not bool(
                    paragraph.get("suggestedParagraphStyleChanges")
                )
                if not isinstance(paragraph_style, dict):
                    paragraph_style = {}
                named_style_type = paragraph_style.get("namedStyleType")
                if named_style_type is not None and not isinstance(named_style_type, str):
                    complete = False
                    named_style_type = None
                paragraphs.append(
                    DocParagraph(
                        text,
                        int(start),
                        int(end),
                        tuple(runs),
                        named_style_type,
                        in_table,
                        paragraph_style_supported,
                    )
                )
            elif "table" in structural:
                for row in structural["table"].get("tableRows", ()):
                    row_suggested = suggested or has_suggestions(row)
                    if row_suggested:
                        complete = False
                    for cell in row.get("tableCells", ()):
                        cell_suggested = row_suggested or has_suggestions(cell)
                        if cell_suggested:
                            complete = False
                        collect_content(cell.get("content", ()), cell_suggested, True)
            elif "tableOfContents" in structural:
                collect_content(
                    structural["tableOfContents"].get("content", ()), suggested, in_table
                )
            elif "sectionBreak" not in structural:
                complete = False

    collect_content((body_tab.get("body") or {}).get("content", ()))
    return DocSnapshot(
        file_id=file_id,
        revision_id=revision,
        tab_id=resolved_tab_id,
        title=str(props.get("title") or ""),
        text="".join(item.text for item in paragraphs),
        paragraphs=tuple(paragraphs),
        complete=complete,
        named_styles=tuple(style_rows),
        style_context_supported=style_context_supported,
    )


def find_doc_match(snapshot: DocSnapshot, find: str) -> DocMatch:
    if not isinstance(find, str) or not find or "\n" in find or "\r" in find:
        raise ValueError("The requested match must be nonempty text within one paragraph boundary.")
    full_count = _occurrences(snapshot.text, find)
    if full_count == 0:
        raise ValueError("The requested text was not found in the selected tab's main body.")
    if full_count != 1:
        raise ValueError("The requested text must have exactly one match in the selected tab.")
    for paragraph in snapshot.paragraphs:
        at = paragraph.text.find(find)
        if at < 0:
            continue
        local_end = at + len(find)
        overlapping = []
        structural_ranges = []
        start = end = None
        for run in paragraph.runs:
            if run.get("structural"):
                a, b = run.get("startIndex"), run.get("endIndex")
                if a is not None and b is not None:
                    structural_ranges.append((int(a), int(b)))
                continue
            run_start, run_end = run.get("start_index"), run.get("end_index")
            if run_start is None or run_end is None:
                raise ValueError("Google returned a text run without stable UTF-16 indexes.")
            run_start, run_end = int(run_start), int(run_end)
            # Translate this run's local Python offset by the known prefix in the paragraph.
            char_cursor = 0
            for prior in paragraph.runs:
                if prior is run:
                    break
                if not prior.get("structural"):
                    char_cursor += len(prior["text"])
            overlap_start = max(at, char_cursor)
            overlap_end = min(local_end, char_cursor + len(run["text"]))
            if overlap_start >= overlap_end:
                continue
            if run["suggestions"]:
                raise ValueError("The requested range touches a pending suggestion.")
            if start is None:
                start = run_start + utf16_length(run["text"][: overlap_start - char_cursor])
            end = run_start + utf16_length(run["text"][: overlap_end - char_cursor])
            overlapping.append(run)
        if not overlapping or start is None or end is None:
            raise ValueError("The requested range has no supported text style data.")
        if any(a < end and b > start for a, b in structural_ranges):
            raise ValueError("The requested range contains unsupported structural content.")
        # A suggestion earlier in the same selected tab can change Docs indices in inline mode.
        for earlier in snapshot.paragraphs:
            for run in earlier.runs:
                if run.get("suggestions") and (
                    earlier.end_index <= start
                    or earlier is paragraph
                    and run.get("end_index", 0) <= start
                ):
                    raise ValueError("Pending suggestions before the match make its index unsafe.")
            if earlier is paragraph:
                break
        styles = [run["style"] for run in overlapping]
        if any(style != styles[0] for style in styles[1:]):
            raise ValueError("The requested range has mixed styling.")
        supported_style = {
            "bold",
            "italic",
            "underline",
            "strikethrough",
            "smallCaps",
            "backgroundColor",
            "foregroundColor",
            "baselineOffset",
            "fontSize",
            "weightedFontFamily",
            "link",
        }
        if set(styles[0]) - supported_style:
            raise ValueError("The requested range uses unsupported text styling.")
        if end - start != utf16_length(find):
            raise ValueError("Google returned inconsistent UTF-16 indexes for the requested range.")
        effective_style, resolved = resolve_effective_doc_style(
            styles[0], snapshot.named_styles, paragraph.named_style_type
        )
        if paragraph.in_table:
            effective_style, resolved = {}, False
        if not snapshot.style_context_supported or not paragraph.style_context_supported:
            effective_style, resolved = {}, False
        return DocMatch(
            find,
            start,
            end,
            snapshot.tab_id,
            dict(styles[0]),
            effective_style,
            resolved,
            paragraph.named_style_type,
            paragraph.in_table,
            paragraph.style_context_supported and snapshot.style_context_supported,
        )
    raise ValueError("The requested text must stay within one paragraph boundary.")


def doc_style_for_range(snapshot: DocSnapshot, start_index: int, end_index: int) -> dict:
    """Read back one uniformly styled UTF-16 range, rejecting uncovered/structural spans."""
    if start_index < 0 or end_index <= start_index:
        raise ValueError("The selected text style range is invalid.")
    candidates = []
    for paragraph in snapshot.paragraphs:
        structural = []
        for run in paragraph.runs:
            if run.get("structural"):
                a, b = run.get("startIndex"), run.get("endIndex")
                if a is not None and b is not None:
                    structural.append((int(a), int(b)))
                continue
            a, b = run.get("start_index"), run.get("end_index")
            if a is None or b is None or int(a) >= end_index or int(b) <= start_index:
                continue
            if run["suggestions"]:
                raise ValueError("The selected text style range touches a suggestion.")
            candidates.append((max(start_index, int(a)), min(end_index, int(b)), run["style"]))
        if any(a < end_index and b > start_index for a, b in structural):
            raise ValueError("The selected text style range contains structural content.")
    candidates.sort(key=lambda item: item[0])
    cursor = start_index
    for left, right, _ in candidates:
        if left > cursor:
            raise ValueError("Google did not return complete styling coverage for the edited text.")
        cursor = max(cursor, right)
    if cursor < end_index or not candidates:
        raise ValueError("Google did not return complete styling coverage for the edited text.")
    style = candidates[0][2]
    if any(value != style for _, _, value in candidates[1:]):
        raise ValueError("The saved replacement has mixed text styling.")
    return dict(style)


_SUPPORTED_DOC_STYLE = frozenset(
    {
        "bold",
        "italic",
        "underline",
        "strikethrough",
        "smallCaps",
        "backgroundColor",
        "foregroundColor",
        "baselineOffset",
        "fontSize",
        "weightedFontFamily",
        "link",
    }
)


def resolve_effective_doc_style(raw_style, named_styles, named_style_type):
    """Resolve only explicitly evidenced style values; unknown inheritance stays unknown."""
    if not isinstance(raw_style, dict) or set(raw_style) - _SUPPORTED_DOC_STYLE:
        return {}, False
    inherited = {}
    if named_style_type is not None:
        if not isinstance(named_styles, tuple | list):
            return {}, False
        normal = [row for row in named_styles if row.get("namedStyleType") == "NORMAL_TEXT"]
        if len(normal) != 1 or not isinstance(normal[0].get("textStyle"), dict):
            return {}, False
        named = [row for row in named_styles if row.get("namedStyleType") == named_style_type]
        if len(named) != 1 or not isinstance(named[0].get("textStyle"), dict):
            return {}, False
        normal_style = {k: v for k, v in normal[0]["textStyle"].items() if k != "link"}
        named_style = {k: v for k, v in named[0]["textStyle"].items() if k != "link"}
        if set(normal_style) - _SUPPORTED_DOC_STYLE or set(named_style) - _SUPPORTED_DOC_STYLE:
            return {}, False
        inherited = _merge_style_layers(normal_style, named_style)
    else:
        return {}, False
    resolved = _merge_style_layers(inherited, raw_style)
    if not resolved:
        return {}, False
    resolved.setdefault("link", None)
    required = _SUPPORTED_DOC_STYLE - {"link"}
    if not required.issubset(resolved):
        return {}, False
    try:
        resolved = {key: _normalize_style_value(key, value) for key, value in resolved.items()}
    except (TypeError, ValueError):
        return {}, False
    return resolved, True


def _merge_style_layers(*layers):
    result = {}
    for layer in layers:
        for key, value in layer.items():
            if key == "baselineOffset" and value == "BASELINE_OFFSET_UNSPECIFIED":
                continue
            result[key] = value
    return result


def _normalize_style_value(key, value):
    booleans = {"bold", "italic", "underline", "strikethrough", "smallCaps"}
    if key in booleans:
        if not isinstance(value, bool):
            raise ValueError("Invalid boolean text style.")
        return value
    if key in {"backgroundColor", "foregroundColor"}:
        if value == {}:
            return {"transparent": True}
        if not isinstance(value, dict) or set(value) != {"color"}:
            raise ValueError("Unsupported optional color.")
        color = value["color"]
        if not isinstance(color, dict) or set(color) != {"rgbColor"}:
            raise ValueError("Unsupported color type.")
        rgb = color["rgbColor"]
        if not isinstance(rgb, dict) or set(rgb) - {"red", "green", "blue"}:
            raise ValueError("Malformed RGB color.")
        if any(
            isinstance(component, bool)
            or not isinstance(component, int | float)
            or not math.isfinite(component)
            or not 0 <= component <= 1
            for component in rgb.values()
        ):
            raise ValueError("Invalid RGB component.")
        return {channel: float(rgb.get(channel, 0)) for channel in ("red", "green", "blue")}
    if key == "fontSize":
        if (
            not isinstance(value, dict)
            or set(value) != {"magnitude", "unit"}
            or value["unit"] != "PT"
            or isinstance(value["magnitude"], bool)
            or not isinstance(value["magnitude"], int | float)
            or not math.isfinite(value["magnitude"])
            or value["magnitude"] <= 0
        ):
            raise ValueError("Unsupported font size.")
        return float(value["magnitude"])
    if key == "weightedFontFamily":
        if not isinstance(value, dict) or set(value) - {"fontFamily", "weight"}:
            raise ValueError("Unsupported font family.")
        family, weight = value.get("fontFamily"), value.get("weight", 400)
        if (
            not isinstance(family, str)
            or not family
            or isinstance(weight, bool)
            or not isinstance(weight, int)
            or weight < 100
            or weight > 900
            or weight % 100
        ):
            raise ValueError("Invalid font family.")
        return {"fontFamily": family, "weight": weight}
    if key == "baselineOffset":
        if value not in {"NONE", "SUPERSCRIPT", "SUBSCRIPT"}:
            raise ValueError("Unsupported baseline offset.")
        return value
    if key == "link":
        if value is None:
            return None
        if not isinstance(value, dict) or len(value) != 1:
            raise ValueError("Unsupported link target.")
        target, target_value = next(iter(value.items()))
        if (
            target not in {"url", "bookmarkId", "headingId", "tabId"}
            or not isinstance(target_value, str)
            or not target_value
        ):
            raise ValueError("Invalid link target.")
        return {target: target_value}
    raise ValueError("Unsupported text style.")


def paragraph_style_for_range(snapshot, start_index, end_index):
    matches = [
        paragraph
        for paragraph in snapshot.paragraphs
        if paragraph.start_index <= start_index and paragraph.end_index >= end_index
    ]
    if len(matches) != 1:
        return None, False
    paragraph = matches[0]
    return (
        paragraph.named_style_type,
        not paragraph.in_table
        and paragraph.style_context_supported
        and snapshot.style_context_supported,
    )


_A1_RANGE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)(?::([A-Z]{1,3})([1-9][0-9]*))?$")


def _col_number(value):
    output = 0
    for char in value:
        output = output * 26 + ord(char) - 64
    return output


def _typed_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, (int, float)) and math.isfinite(value):
        return {"numberValue": value}
    raise ValueError(
        "Sheet edits accept only literal strings, numbers, booleans and explicit blanks."
    )


def _value_type(value):
    return next(iter(value), "empty") if value else "empty"


@dataclass(frozen=True)
class SheetEditSnapshot:
    sheet_id: int
    tab: str
    cell_range: str
    grid_range: dict
    before: tuple[tuple[dict | None, ...], ...]
    after: tuple[tuple[dict | None, ...], ...]
    type_changes: tuple[tuple[bool, ...], ...]


def _intersects(left, right):
    if left.get("sheetId") != right.get("sheetId"):
        return False
    start_row = int(right.get("startRowIndex", 0))
    end_row = int(right["endRowIndex"]) if right.get("endRowIndex") is not None else math.inf
    start_col = int(right.get("startColumnIndex", 0))
    end_col = int(right["endColumnIndex"]) if right.get("endColumnIndex") is not None else math.inf
    return (
        int(left["startRowIndex"]) < end_row
        and int(left["endRowIndex"]) > start_row
        and int(left["startColumnIndex"]) < end_col
        and int(left["endColumnIndex"]) > start_col
    )


def sheet_range_shape(cell_range, *, max_cells, max_rows, max_columns):
    match = _A1_RANGE.fullmatch(cell_range) if isinstance(cell_range, str) else None
    if not match:
        raise ValueError("Use a bounded A1 range such as G5:H5 without a tab prefix.")
    c1, r1, c2, r2 = match.groups()
    c2, r2 = c2 or c1, r2 or r1
    start_col, start_row = _col_number(c1), int(r1)
    end_col, end_row = _col_number(c2) + 1, int(r2)
    if end_col <= start_col or end_row < start_row:
        raise ValueError("The Sheet range must be rectangular and ordered.")
    width, height = end_col - start_col, end_row - start_row + 1
    if height > max_rows or width > max_columns:
        raise ValueError("The Sheet range exceeds the configured row or column limit.")
    if width * height > max_cells:
        raise ValueError("The Sheet edit exceeds the configured cell limit.")
    return (
        width,
        height,
        {
            "startRowIndex": start_row - 1,
            "endRowIndex": end_row,
            "startColumnIndex": start_col - 1,
            "endColumnIndex": end_col - 1,
        },
    )


def validate_sheet_values(values, *, width, height, max_cells, max_cell_chars, max_chars):
    if width * height > max_cells:
        raise ValueError("The Sheet edit exceeds the configured cell limit.")
    if (
        not isinstance(values, list)
        or len(values) != height
        or any(not isinstance(row, list) or len(row) != width for row in values)
    ):
        raise ValueError("The proposed values must have the same dimensions as the selected range.")
    after = []
    total_chars = 0
    for row in values:
        encoded = []
        for value in row:
            if isinstance(value, str) and len(value) > max_cell_chars:
                raise ValueError("A proposed cell exceeds the character limit.")
            encoded.append(_typed_value(value))
            total_chars += len(value) if isinstance(value, str) else len(str(value))
        after.append(tuple(encoded))
    if total_chars > max_chars:
        raise ValueError("The proposed Sheet values exceed the content limit.")
    return tuple(after)


def build_sheet_edit(
    sheet,
    *,
    sheet_id,
    tab,
    cell_range,
    values,
    max_cells,
    max_cell_chars,
    max_rows=1000,
    max_columns=100,
    max_chars=100_000,
):
    """Validate one literal rectangular update and freeze typed before-values."""
    width, height, grid_bounds = sheet_range_shape(
        cell_range,
        max_cells=max_cells,
        max_rows=max_rows,
        max_columns=max_columns,
    )
    start_col = grid_bounds["startColumnIndex"] + 1
    start_row = grid_bounds["startRowIndex"] + 1
    end_col = grid_bounds["endColumnIndex"] + 1
    end_row = grid_bounds["endRowIndex"]
    after = validate_sheet_values(
        values,
        width=width,
        height=height,
        max_cells=max_cells,
        max_cell_chars=max_cell_chars,
        max_chars=max_chars,
    )

    candidates = []
    for item in sheet.get("sheets", ()):
        props = item.get("properties") or {}
        if props.get("sheetId") == sheet_id:
            candidates.append(item)
    if len(candidates) != 1 or candidates[0].get("properties", {}).get("title") != tab:
        raise ValueError("The selected Sheet tab changed; request a fresh preview.")
    item = candidates[0]
    bounds = item.get("properties", {}).get("gridProperties") or {}
    if end_col - 1 > int(bounds.get("columnCount", 0)) or end_row > int(bounds.get("rowCount", 0)):
        raise ValueError("The selected Sheet range exceeds the tab grid.")
    grid = {"sheetId": sheet_id, **grid_bounds}
    if any(_intersects(grid, value) for value in item.get("merges", ())):
        raise ValueError("The selected range contains merged cells.")
    named_ranges = {
        value.get("namedRangeId"): value.get("range") for value in sheet.get("namedRanges", ())
    }
    for protected in item.get("protectedRanges", ()):
        protected_grid = protected.get("range")
        if not protected_grid and protected.get("namedRangeId"):
            protected_grid = named_ranges.get(protected["namedRangeId"])
            if protected_grid is None:
                raise ValueError("A named protected range could not be resolved safely.")
        if protected_grid and _intersects(grid, protected_grid):
            raise ValueError("The selected range contains protected cells.")

    typed_rows = [[None for _ in range(width)] for _ in range(height)]
    covered = set()
    for data in item.get("data", ()):
        row_start = int(data.get("startRow", 0))
        col_start = int(data.get("startColumn", 0))
        row_data = data.get("rowData", ())
        for row_idx, row in enumerate(row_data):
            absolute_row = row_start + row_idx
            if not start_row - 1 <= absolute_row < end_row:
                continue
            if not isinstance(row.get("values", ()), list):
                raise ValueError("Google returned invalid target range cell data.")
            cells = row.get("values", ())
            for col_idx, cell in enumerate(cells):
                absolute_col = col_start + col_idx
                if not start_col - 1 <= absolute_col < end_col:
                    continue
                covered.add((absolute_row, absolute_col))
                entered = cell.get("userEnteredValue")
                effective = cell.get("effectiveValue")
                if not entered and effective:
                    raise ValueError("The selected range contains spill output cells.")
                if entered and "formulaValue" in entered:
                    raise ValueError("Replacing a formula cell is not supported.")
                if entered and (
                    len(entered) != 1
                    or next(iter(entered)) not in {"stringValue", "numberValue", "boolValue"}
                ):
                    raise ValueError("The selected range contains an unsupported cell type.")
                typed_rows[absolute_row - (start_row - 1)][absolute_col - (start_col - 1)] = (
                    dict(entered) if entered else None
                )
    expected_cells = {
        (row_idx, col_idx)
        for row_idx in range(start_row - 1, end_row)
        for col_idx in range(start_col - 1, end_col - 1)
    }
    if covered != expected_cells:
        raise ValueError("Google did not return complete typed coverage of the selected range.")
    before = tuple(tuple(row) for row in typed_rows)
    after_tuple = after
    type_changes = tuple(
        tuple(
            _value_type(old) != _value_type(new)
            for old, new in zip(before_row, after_row, strict=True)
        )
        for before_row, after_row in zip(before, after_tuple, strict=True)
    )
    return SheetEditSnapshot(sheet_id, tab, cell_range, grid, before, after_tuple, type_changes)
