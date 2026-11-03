"""Bounded CSV parsing and resource-isolated PDF text extraction."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sys
from pathlib import Path

PDF_TIMEOUT_S = 5
_ABSOLUTE_MAX_BYTES = 10_000_000


def read_csv(data: bytes, settings) -> str:
    """Return literal cells with explicit coverage; no formulas are evaluated."""
    if len(data) > settings.max_bytes:
        raise ValueError("CSV exceeds the download size limit.")
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("CSV must use UTF-8 or UTF-8 with a BOM.") from None
    if not decoded.strip():
        raise ValueError("CSV is empty.")
    if "\0" in decoded:
        raise ValueError("CSV contains invalid binary characters.")
    rows = []
    partial = False
    # Reserve the small coverage header, retaining complete rows and valid JSON.
    remaining = settings.max_chars - 90
    try:
        reader = csv.reader(io.StringIO(decoded, newline=""), delimiter=",", strict=True)
        for row in reader:
            if not row:
                continue
            if len(rows) >= settings.max_rows:
                partial = True
                break
            if len(row) > settings.max_columns:
                raise ValueError("CSV row exceeds the column limit.")
            if any(len(cell) > settings.max_cell_chars for cell in row):
                raise ValueError("CSV cell exceeds the character limit.")
            row_size = len(json.dumps(row, ensure_ascii=False)) + 2
            if row_size > remaining:
                partial = True
                break
            rows.append(row)
            remaining -= row_size
    except csv.Error:
        raise ValueError("CSV is malformed or contains an oversized field.") from None
    if not rows and not partial:
        raise ValueError("CSV is empty.")
    return (
        f"CSV rows read: {len(rows)}; partial: {str(partial).lower()}. Cells are literal text.\n"
        + json.dumps(rows, ensure_ascii=False)
    )


async def read_pdf(data: bytes, *, max_chars: int) -> str:
    """Parse outside the bot process; deadline/cancellation always kill and reap."""
    if not 256 <= max_chars <= 100_000 or len(data) > _ABSOLUTE_MAX_BYTES:
        raise ValueError("PDF exceeds the supported input/output limits.")
    limit = max_chars * 4 + 1024
    # The interpreter path is absolute. Isolated mode and this environment keep
    # bot credentials/configuration out of the parser's process environment.
    environment = {key: os.environ[key] for key in ("SYSTEMROOT",) if key in os.environ}
    process = None
    tasks = []
    try:
        async with asyncio.timeout(PDF_TIMEOUT_S):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(Path(__file__).with_name("pdf_worker.py")),
                str(max_chars),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )

            async def feed():
                process.stdin.write(data)
                await process.stdin.drain()
                process.stdin.close()

            async def capture():
                output = bytearray()
                while chunk := await process.stdout.read(min(65536, limit + 1 - len(output))):
                    output.extend(chunk)
                    if len(output) > limit:
                        raise ValueError("PDF parser output exceeds the size limit.")
                return bytes(output)

            tasks = [asyncio.create_task(feed()), asyncio.create_task(capture())]
            _, raw, _ = await asyncio.gather(*tasks, process.wait())
        if process.returncode != 0:
            raise ValueError("PDF could not be read within the parser resource limits.")
        try:
            result = json.loads(raw)
        except (UnicodeError, ValueError):
            raise ValueError("PDF parser returned an invalid result.") from None
        if not isinstance(result, dict):
            raise ValueError("PDF parser returned an invalid result.")
        if result.get("error") in {"encrypted", "malformed"}:
            if result["error"] == "encrypted":
                raise ValueError("PDF is encrypted; attach an unencrypted copy to read it.")
            raise ValueError("PDF is malformed or cannot be read within the parser limits.")
        text = result.get("text")
        if not isinstance(text, str) or len(text) > max_chars:
            raise ValueError("PDF parser returned an invalid result.")
        return text
    except TimeoutError:
        raise ValueError("PDF text extraction ran out of time; try a smaller file.") from None
    except (OSError, BrokenPipeError, ConnectionResetError):
        raise ValueError("PDF text extraction is unavailable; try another file.") from None
    finally:
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            if process.stdin is not None:
                process.stdin.close()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await process.wait()
