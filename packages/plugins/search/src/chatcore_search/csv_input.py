"""Bounded CSV attachment parsing; cells are data, never formulas or instructions."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class CsvInput:
    rows: list[list[str]]
    truncated: bool = False

    def render(self) -> str:
        text = "CSV rows (first row preserved as supplied):\n" + json.dumps(
            self.rows, ensure_ascii=False
        )
        return text + ("\n[Truncated: partial CSV coverage]" if self.truncated else "")


def parse_csv(
    data: bytes,
    *,
    max_bytes: int = 2_000_000,
    max_rows: int = 500,
    max_columns: int = 32,
    max_field_chars: int = 10_000,
    max_chars: int = 45_000,
) -> CsvInput:
    if len(data) > max_bytes:
        raise ValueError("CSV exceeds the size limit")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV must use UTF-8 encoding") from None
    if "\x00" in text:
        raise ValueError("CSV contains invalid null bytes")
    rows: list[list[str]] = []
    truncated = False
    chars = 0
    try:
        for row in csv.reader(io.StringIO(text, newline=""), strict=True):
            if len(rows) >= max_rows:
                truncated = True
                break
            if any(len(cell) > max_field_chars for cell in row):
                raise ValueError("CSV field exceeds the field limit")
            if len(row) > max_columns:
                truncated = True
                row = row[:max_columns]
            row_chars = len(json.dumps(row, ensure_ascii=False)) + 2
            if chars + row_chars > max_chars:
                truncated = True
                break
            rows.append(row)
            chars += row_chars
    except csv.Error:
        raise ValueError("CSV is malformed or contains an oversized field") from None
    return CsvInput(rows, truncated)
