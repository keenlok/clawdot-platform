"""Validate a neutral workbook model and render bounded Google Sheets API requests.

The model (``schema`` 1) is produced by a provider such as ``chatcore_expenses.workbook``;
this module never imports it. Accepted cell kinds are ``text``, ``number``, ``rate``,
``money`` (``exp`` decimals), ``percent``, ``bool``, ``url``, ``date``, ``datetime`` and
``sum`` (a same-column ``SUM`` over rows above). Text is always written as
``userEnteredValue.stringValue``, which Sheets stores
as literal text and never parses (verified against the live API: ``=HYPERLINK(...)`` stays a
string). No apostrophe prefix is added, because in ``stringValue`` it would be displayed;
numbers and dates are typed values. The only formulas are ``SUM`` totals generated here from
row indices, so ledger text cannot become a formula.
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from chatcore_google.service import column_name

logger = logging.getLogger(__name__)

SCHEMA = 1
MAX_SHEETS = 80
MAX_COLUMNS = 200
CELLS_PER_REQUEST = 5000
MAX_BATCH_BYTES = 2_000_000
MAX_BATCH_REQUESTS = 100
_EPOCH = date(1899, 12, 30)
_KINDS = {"text", "number", "rate", "money", "percent", "bool", "url", "date", "datetime", "sum"}
_STYLES = {"title", "note", "warn", "header", "total", "wrap", "pos", "neg"}
_NAVY = {"red": 0.137, "green": 0.239, "blue": 0.361}
_WHITE = {"red": 1, "green": 1, "blue": 1}
_BAND = {"red": 0.945, "green": 0.957, "blue": 0.973}
_TOTAL = {"red": 0.878, "green": 0.906, "blue": 0.941}
_STYLE_FORMATS = {
    "title": {"textFormat": {"bold": True, "fontSize": 14, "foregroundColor": _NAVY}},
    "note": {"textFormat": {"foregroundColor": {"red": 0.35, "green": 0.35, "blue": 0.35}}},
    "warn": {
        "textFormat": {"bold": True, "foregroundColor": {"red": 0.6, "green": 0.1, "blue": 0.1}}
    },
    "header": {
        "backgroundColor": _NAVY,
        "textFormat": {"bold": True, "foregroundColor": _WHITE},
        "wrapStrategy": "WRAP",
        "verticalAlignment": "MIDDLE",
        "horizontalAlignment": "CENTER",
    },
    "total": {"backgroundColor": _TOTAL, "textFormat": {"bold": True}},
    "wrap": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"},
    "pos": {
        "textFormat": {"bold": True, "foregroundColor": {"red": 0.1, "green": 0.45, "blue": 0.2}}
    },
    "neg": {
        "textFormat": {"bold": True, "foregroundColor": {"red": 0.65, "green": 0.1, "blue": 0.1}}
    },
}


@dataclass(frozen=True, slots=True)
class WorkbookPlan:
    """A create body, data batches and a final formatting batch for one new spreadsheet."""

    create: dict
    batches: list[list[dict]]
    finish: list[dict]
    title: str
    cells: int


def money_pattern(exp: int) -> str:
    body = "#,##0" + ("." + "0" * exp if exp else "")
    return f'{body};[Red]({body});"–"'


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Workbook numbers must be exact decimal strings.")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("Workbook number is not a decimal.") from None
    if not number.is_finite() or abs(number) > Decimal(10) ** 15:
        raise ValueError("Workbook number is out of the spreadsheet's exact range.")
    return number


def _serial(value: Any, *, with_time: bool) -> float:
    if not isinstance(value, str):
        raise ValueError("Workbook dates must be ISO strings.")
    try:
        if with_time:
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is not None:
                raise ValueError("Workbook times must be local wall times.")
            delta = moment - datetime(1899, 12, 30)
            return delta.days + delta.seconds / 86400
        return float((date.fromisoformat(value) - _EPOCH).days)
    except ValueError:
        raise ValueError("Workbook date is not valid ISO format.") from None


def _int(value: Any, low: int, high: int, what: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Workbook {what} is out of range.")
    return value


def _cell(cell: Any, row: int, column: int, max_cell_chars: int) -> dict:
    if cell is None:
        return {}
    if not isinstance(cell, dict) or cell.get("k") not in _KINDS:
        raise ValueError("Workbook cell has an unknown kind.")
    unknown = set(cell) - {"k", "v", "s", "exp", "as"}
    if unknown:
        raise ValueError("Workbook cell has unknown fields.")
    kind, value, style = cell["k"], cell.get("v"), cell.get("s")
    if "as" in cell and kind != "sum":
        raise ValueError("Workbook total format is only valid for sum cells.")
    if style is not None and style not in _STYLES:
        raise ValueError("Workbook cell has an unknown style.")
    fmt: dict = dict(_STYLE_FORMATS.get(style, {}))
    if kind == "text":
        if not isinstance(value, str) or len(value) > max_cell_chars:
            raise ValueError("Workbook text cell is invalid or too long.")
        entered = {"stringValue": value}
    elif kind == "sum":
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(type(item) is int for item in value)
            or not 0 <= value[0] <= value[1] < row
        ):
            raise ValueError("Workbook total must sum rows above it.")
        letter = column_name(column + 1)
        entered = {"formulaValue": f"=SUM({letter}{value[0] + 1}:{letter}{value[1] + 1})"}
        as_kind = cell.get("as", "money")
        if as_kind == "money":
            fmt["numberFormat"] = {
                "type": "NUMBER",
                "pattern": money_pattern(_int(cell.get("exp"), 0, 4, "precision")),
            }
        elif as_kind == "number":
            if "exp" in cell:
                raise ValueError("Workbook number total cannot set precision.")
            fmt["numberFormat"] = {"type": "NUMBER", "pattern": "#,##0.########"}
        elif as_kind == "percent":
            if "exp" in cell:
                raise ValueError("Workbook percent total cannot set precision.")
            fmt["numberFormat"] = {"type": "PERCENT", "pattern": "0.0%"}
        else:
            raise ValueError("Workbook total format is invalid.")
    elif kind in {"date", "datetime"}:
        entered = {"numberValue": _serial(value, with_time=kind == "datetime")}
        fmt["numberFormat"] = (
            {"type": "DATE", "pattern": "dd mmm yyyy"}
            if kind == "date"
            else {"type": "DATE_TIME", "pattern": "dd mmm yyyy hh:mm"}
        )
    elif kind == "bool":
        if type(value) is not bool:
            raise ValueError("Workbook checkbox cell must be true or false.")
        entered = {"boolValue": value}
    elif kind == "url":
        if (
            not isinstance(value, str)
            or len(value) > min(max_cell_chars, 2000)
            or any(char.isspace() or unicodedata.category(char) == "Cc" for char in value)
        ):
            raise ValueError("Workbook URL is invalid or too long.")
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise ValueError("Workbook URL is invalid.") from None
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Workbook URL must be an http or https URL with a host.")
        entered = {"stringValue": value}
    else:
        number = _decimal(value)
        as_float = float(number)
        if not math.isfinite(as_float):
            raise ValueError("Workbook number is not finite.")
        entered = {"numberValue": as_float}
        if kind == "percent":
            fmt["numberFormat"] = {"type": "PERCENT", "pattern": "0.0%"}
        elif kind == "money":
            pattern = money_pattern(_int(cell.get("exp"), 0, 4, "precision"))
            fmt["numberFormat"] = {"type": "NUMBER", "pattern": pattern}
        elif kind == "rate":
            fmt["numberFormat"] = {"type": "NUMBER", "pattern": "0.0#########"}
        else:
            fmt["numberFormat"] = {
                "type": "NUMBER",
                "pattern": "#,##0" if number == number.to_integral_value() else "#,##0.########",
            }
    result = {"userEnteredValue": entered}
    if kind == "bool":
        result["dataValidation"] = {"condition": {"type": "BOOLEAN"}}
    if kind == "url":
        result["textFormatRuns"] = [{"startIndex": 0, "format": {"link": {"uri": value}}}]
    if fmt:
        result["userEnteredFormat"] = fmt
    return result


def render_workbook(model: Any, *, title: str, max_cells: int, max_cell_chars: int) -> WorkbookPlan:
    """Validate ``model`` and return bounded requests; raise ``ValueError`` before any write."""
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("Provide a title of 1–200 characters.")
    if not isinstance(model, dict) or model.get("schema") != SCHEMA:
        raise ValueError("Unsupported workbook format.")
    sheets = model.get("sheets")
    if not isinstance(sheets, list) or not 1 <= len(sheets) <= MAX_SHEETS:
        raise ValueError("The workbook has too many or no tabs.")
    seen: set[str] = set()
    properties, data, finish = [], [], []
    total_cells = 0
    for index, sheet in enumerate(sheets):
        if not isinstance(sheet, dict):
            raise ValueError("Workbook tab is invalid.")
        name = sheet.get("title")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 100
            or name != name.strip()
            or any(not char.isprintable() for char in name)
            or name.casefold() in seen
        ):
            raise ValueError("Workbook tab names must be unique, printable and 1–100 characters.")
        seen.add(name.casefold())
        rows = sheet.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, list) for row in rows):
            raise ValueError("Workbook rows are invalid.")
        widths = sheet.get("widths", [])
        tables = sheet.get("tables", [])
        if not isinstance(widths, list) or not isinstance(tables, list):
            raise ValueError("Workbook layout is invalid.")
        row_count = max(1, len(rows))
        column_count = max([1, len(widths), *(len(row) for row in rows)])
        if column_count > MAX_COLUMNS:
            raise ValueError(f"A workbook tab exceeds {MAX_COLUMNS} columns.")
        total_cells += row_count * column_count
        if total_cells > max_cells:
            raise ValueError(
                f"This export needs more than {max_cells:,} cells; export a smaller trip or ask "
                "the bot owner to raise GOOGLE_EXPORT_MAX_CELLS."
            )
        frozen = _int(sheet.get("frozen_rows", 0), 0, min(10, row_count), "frozen row count")
        sheet_id = index + 1
        grid = {"rowCount": row_count, "columnCount": column_count}
        if frozen:
            grid["frozenRowCount"] = frozen
        properties.append(
            {
                "properties": {
                    "sheetId": sheet_id,
                    "title": name,
                    "index": index,
                    "gridProperties": grid,
                }
            }
        )
        rendered = [
            {"values": [_cell(cell, r, c, max_cell_chars) for c, cell in enumerate(row)]}
            for r, row in enumerate(rows)
        ]
        step = max(1, CELLS_PER_REQUEST // column_count)
        for start in range(0, len(rendered), step):
            request_rows = rendered[start : start + step]
            kinds = {cell.get("k") for row in rows[start : start + step] for cell in row if cell}
            fields = "userEnteredValue,userEnteredFormat"
            if "bool" in kinds:
                fields += ",dataValidation"
            if "url" in kinds:
                fields += ",textFormatRuns"
            data.append(
                {
                    "updateCells": {
                        "start": {"sheetId": sheet_id, "rowIndex": start, "columnIndex": 0},
                        "rows": request_rows,
                        "fields": fields,
                    }
                }
            )
        filters = 0
        for table in tables:
            if not isinstance(table, dict):
                raise ValueError("Workbook table is invalid.")
            header = _int(table.get("header"), 0, row_count - 1, "table header")
            last = _int(table.get("last"), header, row_count - 1, "table end")
            columns = _int(table.get("columns"), 1, column_count, "table width")
            span = {
                "sheetId": sheet_id,
                "startRowIndex": header,
                "endRowIndex": last + 1,
                "startColumnIndex": 0,
                "endColumnIndex": columns,
            }
            finish.append(
                {
                    "addBanding": {
                        "bandedRange": {
                            "range": span,
                            "rowProperties": {
                                "headerColor": _NAVY,
                                "firstBandColor": _WHITE,
                                "secondBandColor": _BAND,
                            },
                        }
                    }
                }
            )
            if table.get("filter") is True:
                filters += 1
                if filters > 1:
                    raise ValueError("A workbook tab can have only one filter.")
                finish.append({"setBasicFilter": {"filter": {"range": span}}})
        for column, width in enumerate(widths):
            finish.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": column,
                            "endIndex": column + 1,
                        },
                        "properties": {"pixelSize": _int(width, 20, 1000, "column width")},
                        "fields": "pixelSize",
                    }
                }
            )
    finish.append(
        {"updateSpreadsheetProperties": {"properties": {"title": title}, "fields": "title"}}
    )
    create = {
        "properties": {"title": (title[:186] + " (incomplete)")},
        "sheets": properties,
    }
    return WorkbookPlan(create, _batches(data), finish, title, total_cells)


def _batches(requests: list[dict]) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for request in requests:
        request_size = len(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
        if current and (
            size + request_size > MAX_BATCH_BYTES or len(current) >= MAX_BATCH_REQUESTS
        ):
            batches.append(current)
            current, size = [], 0
        current.append(request)
        size += request_size
    if current:
        batches.append(current)
    return batches


async def write_workbook(client, plan: WorkbookPlan, *, on_created=None) -> tuple[str, str | None]:
    """Create the spreadsheet and fill it. Returns ``(url, failure)``; never retries.

    Creation happens first with an "(incomplete)" title; the last batch renames it, so a file
    left behind by any later failure stays visibly incomplete in Drive.
    """
    result = await client.request(
        "POST", "sheets.googleapis.com", "/v4/spreadsheets", json=plan.create
    )
    file_id = result.get("spreadsheetId") if isinstance(result, dict) else None
    if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", file_id):
        raise ValueError("Google did not return the new spreadsheet ID.")
    url = f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
    if on_created is not None:
        try:
            await on_created(file_id)
        except Exception as exc:  # noqa: BLE001 - do not write without its journal row
            logger.warning("workbook output recording failed error_class=%s", type(exc).__name__)
            return url, "recording the new file failed"
    steps = [*plan.batches, plan.finish]
    for number, batch in enumerate(steps, 1):
        try:
            await client.batch(file_id, batch)
        except Exception as exc:  # noqa: BLE001 - report the created file honestly
            logger.warning(
                "workbook export incomplete step=%d of=%d error_class=%s",
                number,
                len(steps),
                type(exc).__name__,
            )
            return url, f"step {number} of {len(steps)} failed"
    return url, None
