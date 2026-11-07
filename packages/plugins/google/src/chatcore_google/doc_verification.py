"""Strict, bounded read-back checks for native Google Docs writes."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from chatcore_google.edit_snapshot import (
    doc_style_for_range,
    flatten_doc_snapshot,
    paragraph_style_for_range,
    resolve_effective_doc_style,
)


@dataclass(frozen=True)
class DocVerification:
    verified: bool
    category: str | None
    attempts: int
    snapshot: object | None = None
    text_verified: bool = False
    formatting_verified: bool = False


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
    last_text_verified = False
    last_formatting_verified = False
    last_snapshot = None
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
            last_text_verified = last_formatting_verified = False
            last_snapshot = None
        else:
            if now() >= deadline:
                return DocVerification(False, "readback_deadline", attempt)
            try:
                snapshot = flatten_doc_snapshot(
                    raw, tab_id=tab_id, max_chars=max_chars, file_id=file_id
                )
            except Exception:
                category = "snapshot_error"
                last_text_verified = last_formatting_verified = False
                last_snapshot = None
            else:
                if require_complete and not snapshot.complete:
                    category = "snapshot_error"
                    last_text_verified = last_formatting_verified = False
                    last_snapshot = None
                elif snapshot.text != expected_text:
                    category = "text_mismatch"
                    last_text_verified = last_formatting_verified = False
                    last_snapshot = snapshot
                elif match is not None and replacement:
                    last_text_verified = True
                    last_snapshot = snapshot
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
                        named_style_type, supported_context = paragraph_style_for_range(
                            snapshot, match.start_index, end
                        )
                        effective, resolved = resolve_effective_doc_style(
                            style, snapshot.named_styles, named_style_type
                        )
                        if not supported_context or not resolved or not match.style_resolved:
                            raise ValueError("The effective text style context is unresolved.")
                    except ValueError:
                        category = "style_coverage"
                        last_formatting_verified = False
                    else:
                        if effective != match.effective_style:
                            category = "style_mismatch"
                            last_formatting_verified = False
                        else:
                            return DocVerification(True, None, attempt, snapshot, True, True)
                else:
                    return DocVerification(True, None, attempt, snapshot, True, True)
        if attempt == 3:
            break
        delay = delays[attempt - 1]
        remaining = deadline - now()
        if remaining <= 0:
            return DocVerification(False, "readback_deadline", attempt)
        await pause(min(delay, remaining))
        if now() >= deadline:
            return DocVerification(False, "readback_deadline", attempt)
    return DocVerification(
        False,
        category,
        3,
        last_snapshot,
        last_text_verified,
        last_formatting_verified,
    )
