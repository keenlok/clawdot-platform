"""Bounded history records with text-based continuation positions."""

import json

from chatcore.attribution import TRUNCATION_MARKER, render_record


def render_history_page(record, *, offset=0):
    text = record["text"]
    if type(offset) is not int or not 0 <= offset <= len(text):
        raise ValueError("offset must be an integer within the message's text length.")
    remaining = text[offset:]
    page = {**record, "text": remaining}
    if offset:
        # Mention positions describe the original message, not a later text slice.
        page["mentions"] = []
    rendered = render_record(page, max_chars=2000)
    if not rendered:
        raise ValueError("The message's attribution is too large to render a history page.")
    decoded = json.loads(rendered)
    shown = decoded["text"]
    truncated = shown != remaining
    consumed = len(shown.removesuffix(TRUNCATION_MARKER)) if truncated else len(shown)
    if truncated and consumed == 0:
        raise ValueError("The message's attribution leaves no room for text; narrow the request.")
    message_id = decoded["message_id"]
    metadata = {
        "message_id": message_id,
        "offset": offset,
        "next_offset": offset + consumed if truncated and message_id else None,
        "total_chars": len(text),
        "truncated": truncated,
        "coverage": "stored message" if message_id else "indexed excerpt only",
    }
    return json.dumps(metadata, ensure_ascii=True, separators=(",", ":")) + "\n" + rendered
