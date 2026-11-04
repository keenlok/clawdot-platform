"""Strict, bounded read-back checks for native Google Docs writes."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from chatcore_google.edit_snapshot import doc_style_for_range, flatten_doc_snapshot


@dataclass(frozen=True)
class DocVerification:
    verified: bool
    category: str | None
    attempts: int
    snapshot: object | None = None


async def verify_doc_readback(
    client,
    file_id,
    expected_text,
    *,
    max_chars,
    tab_id=None,
    match=None,
    replacement=None,
    require_complete=False,
    deadline_s=5.0,
    sleep=None,
    clock=None,
):
    """Try at most three GETs in five seconds; never repeat a mutation."""
    pause = sleep or asyncio.sleep
    now = clock or time.monotonic
    deadline = now() + deadline_s
    delays = (0.1, 0.4)
    category = "read_error"
    for attempt in range(1, 4):
        remaining = deadline - now()
        if remaining <= 0:
            return DocVerification(False, "readback_deadline", attempt - 1)
        try:
            raw = await asyncio.wait_for(client.document_for_edit(file_id), timeout=remaining)
        except TimeoutError:
            return DocVerification(False, "readback_deadline", attempt)
        except Exception:
            category = "read_error"
        else:
            if now() >= deadline:
                return DocVerification(False, "readback_deadline", attempt)
            try:
                snapshot = flatten_doc_snapshot(
                    raw, tab_id=tab_id, max_chars=max_chars, file_id=file_id
                )
            except Exception:
                category = "snapshot_error"
            else:
                if require_complete and not snapshot.complete:
                    category = "snapshot_error"
                elif snapshot.text != expected_text:
                    category = "text_mismatch"
                elif match is not None and replacement:
                    end = match.start_index + len(replacement.encode("utf-16-le")) // 2
                    try:
                        for paragraph in snapshot.paragraphs:
                            if paragraph.start_index > match.start_index:
                                break
                            if any(
                                run.get("suggestions")
                                and int(run.get("end_index", 0)) <= match.start_index
                                for run in paragraph.runs
                            ):
                                raise ValueError(
                                    "Pending suggestions before the edited range make its index "
                                    "unsafe."
                                )
                        style = doc_style_for_range(snapshot, match.start_index, end)
                    except ValueError:
                        category = "style_coverage"
                    else:
                        if style != match.uniform_style:
                            category = "style_mismatch"
                        else:
                            return DocVerification(True, None, attempt, snapshot)
                else:
                    return DocVerification(True, None, attempt, snapshot)
        if attempt == 3:
            break
        delay = delays[attempt - 1]
        remaining = deadline - now()
        if remaining <= 0:
            return DocVerification(False, "readback_deadline", attempt)
        await pause(min(delay, remaining))
        if now() >= deadline:
            return DocVerification(False, "readback_deadline", attempt)
    return DocVerification(False, category, 3)
