"""Literal spreadsheet values and bounded Docs text, including nested document tabs."""

import math
from typing import Any


def neutralize(value: str) -> str:
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


def literal_rows(rows: list[list[Any]]) -> list[dict]:
    result = []
    for row in rows:
        cells = []
        for value in row:
            if value is None:
                literal = {"stringValue": ""}
            elif isinstance(value, str):
                literal = {"stringValue": neutralize(value)}
            elif type(value) is bool:
                literal = {"boolValue": value}
            elif type(value) in (int, float) and abs(value) <= 2**53 and math.isfinite(value):
                literal = {"numberValue": value}
            elif type(value) in (int, float):
                raise ValueError(
                    "A number exceeds the exact spreadsheet range; provide it as text."
                )
            else:
                raise ValueError("Rows must contain only literal text, finite numbers or booleans.")
            cells.append({"userEnteredValue": literal})
        result.append({"values": cells})
    return result


def doc_text(document: dict, *, max_chars: int, tab_id: str | None = None) -> str:
    pieces = []
    remaining = max_chars + 1

    def add(text):
        nonlocal remaining
        if remaining > 0:
            part = str(text)[:remaining]
            pieces.append(part)
            remaining -= len(part)

    def content(items):
        for item in items:
            if remaining <= 0:
                return
            if "paragraph" in item:
                for element in item["paragraph"].get("elements", []):
                    add(element.get("textRun", {}).get("content", ""))
            if "table" in item:
                for row in item["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        content(cell.get("content", []))
                        add("\t")
                    add("\n")
            content(item.get("tableOfContents", {}).get("content", []))

    found = False

    def tabs(items):
        nonlocal found
        for tab in items:
            props = tab.get("tabProperties", {})
            if tab_id is None or props.get("tabId") == tab_id:
                found = True
                add("\n[" + str(props.get("title", "Tab")) + "]\n")
                content(tab.get("documentTab", {}).get("body", {}).get("content", []))
            tabs(tab.get("childTabs", []))

    if document.get("tabs"):
        tabs(document["tabs"])
        if tab_id is not None and not found:
            raise ValueError("That document tab no longer exists.")
    else:
        if tab_id is not None:
            raise ValueError("That document tab no longer exists.")
        content(document.get("body", {}).get("content", []))
    text = "".join(pieces)
    return text[:max_chars] + ("\n[Truncated]" if len(text) > max_chars else "")
