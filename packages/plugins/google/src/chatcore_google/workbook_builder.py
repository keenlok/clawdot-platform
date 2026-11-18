"""Compile model-facing workbook tabs into the bounded neutral renderer schema."""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal

from chatcore_google.workbook import MAX_SHEETS, _cell, _decimal, _serial

_TAB_KEYS = {"name", "heading", "note", "columns", "rows"}
_COLUMN_KEYS = {"header", "type", "decimals", "total", "width"}
_TYPES = {"text", "number", "money", "percent", "date", "datetime", "bool", "url"}


def _error(where: str, message: str):
    raise ValueError(f"{where}: {message}")


def _keys(value, allowed, where):
    if not isinstance(value, dict):
        _error(where, "must be an object.")
    unknown = set(value) - allowed
    if unknown:
        _error(where, f"unknown field {sorted(unknown)[0]!r}.")


def _typed_cell(value, column, tab_name, row_number, max_cell_chars):
    header, kind = column["header"], column["type"]
    where = f'Tab "{tab_name}", row {row_number}, column "{header}"'
    if value is None:
        return None
    if kind == "text":
        if not isinstance(value, str) or len(value) > max_cell_chars:
            _error(where, f"text must be a string of at most {max_cell_chars} characters.")
        cell = {"k": "text", "v": value}
        if len(value) > 60:
            cell["s"] = "wrap"
        return cell
    if kind in {"number", "money", "percent"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _error(where, "must be a JSON number.")
        try:
            number = Decimal(str(value))
            if not number.is_finite() or not math.isfinite(float(number)):
                raise ValueError
            _decimal(format(number, "f"))
        except (ValueError, OverflowError):
            _error(where, "must be a finite number in the spreadsheet's exact range.")
        cell = {"k": kind, "v": format(number, "f")}
        if kind == "money":
            cell["exp"] = column["decimals"]
        return cell
    if kind in {"date", "datetime"}:
        if not isinstance(value, str):
            _error(where, "must be an ISO date/time string.")
        try:
            _serial(value, with_time=kind == "datetime")
            if kind == "date" and date.fromisoformat(value).isoformat() != value:
                raise ValueError
            if kind == "datetime" and not ("T" in value or " " in value):
                raise ValueError
            if kind == "datetime" and datetime.fromisoformat(value).tzinfo is not None:
                raise ValueError
        except ValueError:
            _error(where, "must be a valid ISO date/time (local time for datetime).")
        return {"k": kind, "v": value}
    if kind == "bool":
        if type(value) is not bool:
            _error(where, "must be true or false.")
        return {"k": "bool", "v": value}
    if not isinstance(value, str):
        _error(where, "must be an http or https URL string.")
    try:
        _cell({"k": "url", "v": value}, row_number - 1, 0, max_cell_chars)
    except ValueError as exc:
        _error(where, str(exc))
    return {"k": "url", "v": value}


def compile_workbook(tabs, settings, title="Workbook") -> dict:
    """Validate and compile tool tabs; perform no Google calls or file I/O."""
    try:
        encoded_length = len(json.dumps(tabs, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        raise ValueError("Workbook tabs must be JSON-compatible.") from None
    if encoded_length > settings.output_max_chars:
        raise ValueError(
            f"The workbook input exceeds GOOGLE_OUTPUT_MAX_CHARS ({settings.output_max_chars})."
        )
    max_tabs = min(settings.max_tabs, MAX_SHEETS)
    if not isinstance(tabs, list) or not 1 <= len(tabs) <= max_tabs:
        raise ValueError(f"Provide 1–{max_tabs} workbook tabs.")
    sheets, seen, total_cells = [], set(), 0
    for tab in tabs:
        if not isinstance(tab, dict):
            _error("Workbook tab", "must be an object.")
        name = tab.get("name")
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 100
            or any(not char.isprintable() for char in name.strip())
        ):
            _error("Workbook tab", "name must be 1–100 printable characters.")
        name = name.strip()
        _keys(tab, _TAB_KEYS, f'Tab "{name}"')
        if name.casefold() in seen:
            _error(f'Tab "{name}"', "tab names must be unique case-insensitively.")
        seen.add(name.casefold())
        for key, maximum in (("heading", 200), ("note", 1000)):
            value = tab.get(key)
            if key in tab and (not isinstance(value, str) or len(value) > maximum):
                _error(f'Tab "{name}"', f"{key} must be a string of at most {maximum} characters.")
            if key in tab and len(value) > settings.max_cell_chars:
                _error(
                    f'Tab "{name}"',
                    f"{key} exceeds the {settings.max_cell_chars}-character cell limit.",
                )
        columns = tab.get("columns")
        if not isinstance(columns, list) or not 1 <= len(columns) <= settings.max_columns:
            _error(f'Tab "{name}"', f"provide 1–{settings.max_columns} columns.")
        clean_columns = []
        for column in columns:
            if not isinstance(column, dict):
                _error(f'Tab "{name}" column', "must be an object.")
            location = f'Tab "{name}", column "{column.get("header", "?")}"'
            _keys(column, _COLUMN_KEYS, location)
            header, kind = column.get("header"), column.get("type")
            if not isinstance(header, str) or not header.strip() or len(header) > 200:
                _error(location, "header must be a nonempty string of at most 200 characters.")
            if len(header) > settings.max_cell_chars:
                _error(location, "header exceeds the configured cell character limit.")
            if not isinstance(kind, str) or kind not in _TYPES:
                _error(f'Tab "{name}", column "{header}"', "type is unsupported.")
            if "decimals" in column and kind != "money":
                _error(f'Tab "{name}", column "{header}"', "decimals is only valid for money.")
            decimals = column.get("decimals", 2)
            if kind == "money" and (type(decimals) is not int or not 0 <= decimals <= 4):
                _error(
                    f'Tab "{name}", column "{header}"', "decimals must be an integer from 0 to 4."
                )
            if "total" in column and type(column["total"]) is not bool:
                _error(f'Tab "{name}", column "{header}"', "total must be true or false.")
            if "total" in column and kind not in {"number", "money", "percent"}:
                _error(
                    f'Tab "{name}", column "{header}"',
                    "total is only valid for number, money or percent.",
                )
            width = column.get("width")
            if "width" in column and (type(width) is not int or not 20 <= width <= 1000):
                _error(
                    f'Tab "{name}", column "{header}"', "width must be an integer from 20 to 1000."
                )
            clean_columns.append({**column, "decimals": decimals})
        rows = tab.get("rows")
        if not isinstance(rows, list) or len(rows) > settings.max_rows:
            _error(f'Tab "{name}"', f"rows must be a list of at most {settings.max_rows} rows.")
        for row_number, row in enumerate(rows, 1):
            if not isinstance(row, list) or len(row) != len(columns):
                index = min(len(row) if isinstance(row, list) else 0, len(columns) - 1)
                _error(
                    f'Tab "{name}", row {row_number}, column "{columns[index]["header"]}"',
                    "row must have exactly one value per column.",
                )
        layout_rows = []
        if tab.get("heading"):
            layout_rows.append([{"k": "text", "v": tab["heading"], "s": "title"}])
        if tab.get("note"):
            layout_rows.append([{"k": "text", "v": tab["note"], "s": "note"}])
        if tab.get("heading") or tab.get("note"):
            layout_rows.append([])
        header_index = len(layout_rows)
        layout_rows.append([{"k": "text", "v": c["header"], "s": "header"} for c in clean_columns])
        if not rows:
            layout_rows.append([])
        first_data = len(layout_rows)
        for row_number, row in enumerate(rows, 1):
            layout_rows.append(
                [
                    _typed_cell(value, column, name, row_number, settings.max_cell_chars)
                    for value, column in zip(row, clean_columns, strict=True)
                ]
            )
        last_data = len(layout_rows) - 1 if rows else header_index
        totals = [i for i, column in enumerate(clean_columns) if column.get("total") is True]
        if totals and rows:
            total_row = []
            for index, column in enumerate(clean_columns):
                if index == 0 and index not in totals:
                    total_row.append({"k": "text", "v": "Total", "s": "total"})
                elif index in totals:
                    total = {
                        "k": "sum",
                        "v": [first_data, last_data],
                        "s": "total",
                        "as": column["type"],
                    }
                    if column["type"] == "money":
                        total["exp"] = column["decimals"]
                    total_row.append(total)
                else:
                    total_row.append({"k": "text", "v": "", "s": "total"})
            layout_rows.append(total_row)
        widths = []
        for index, column in enumerate(clean_columns):
            if column.get("width") is not None:
                widths.append(column["width"])
                continue
            lengths = [len(column["header"])]
            for row in rows:
                value = row[index]
                if value is None:
                    continue
                if column["type"] == "date":
                    lengths.append(11)
                elif column["type"] == "datetime":
                    lengths.append(17)
                elif column["type"] == "bool":
                    lengths.append(4)
                else:
                    lengths.append(len(str(value)))
            widths.append(min(400, max(80, 7 * max(lengths) + 24)))
        sheet = {
            "title": name,
            "rows": layout_rows,
            "widths": widths,
            "tables": [
                {"header": header_index, "last": last_data, "columns": len(columns), "filter": True}
            ],
            "frozen_rows": header_index + 1,
        }
        sheets.append(sheet)
        total_cells += max(1, len(layout_rows)) * len(columns)
    if total_cells > settings.export_max_cells:
        raise ValueError(
            f"This workbook needs more than {settings.export_max_cells:,} cells; split it up."
        )
    return {"schema": 1, "title": title, "sheets": sheets}


def preview_workbook(tabs) -> str:
    """Render the compact, bounded human preview for validated tabs."""
    parts = []
    for tab in tabs:
        columns = tab["columns"]
        rows = tab["rows"]
        parts.append(f'Tab "{tab["name"].strip()}": {len(rows)} rows')
        parts.append(", ".join(f"{column['header']} ({column['type']})" for column in columns))
        parts.extend(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows[:3])
        if len(rows) > 3:
            parts.append(f"… {len(rows) - 3} more rows")
    return "\n".join(parts)
