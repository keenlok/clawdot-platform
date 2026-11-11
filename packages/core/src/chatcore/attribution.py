"""Bounded historical speaker and mention metadata for prompts and storage."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from chatcore.events import AttachmentKind, IncomingMessage, Mention, User, UserRef

if TYPE_CHECKING:
    from chatcore.conversation import Turn
    from chatcore.store.base import StoredMessage

MAX_MENTIONS = 16
MAX_DISPLAY_NAME = 128
MAX_USERNAME = 64
MAX_ID = 128
MAX_OFFSET = 1_000_000
TRUNCATION_MARKER = "…[truncated]"


def escape_invisible(value: str) -> str:
    """Escape invisible Unicode as valid JSON-style escapes, including surrogate pairs."""

    def escape(char: str) -> str:
        codepoint = ord(char)
        if not (unicodedata.category(char) in {"Cc", "Cf", "Cs"} or char in "\u2028\u2029"):
            return char
        if codepoint <= 0xFFFF:
            return f"\\u{codepoint:04x}"
        offset = codepoint - 0x10000
        high = 0xD800 + (offset >> 10)
        low = 0xDC00 + (offset & 0x3FF)
        return f"\\u{high:04x}\\u{low:04x}"

    return "".join(escape(char) for char in value)


def _valid_key(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_ID:
        return None
    if any(ord(char) < 32 for char in value):
        return None
    try:
        ref = UserRef.parse(value)
    except (TypeError, ValueError):
        return None
    return value if ref.key == value else None


def _bounded_string(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:maximum]


def _message_id(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_ID:
        return None
    if any(ord(char) < 32 for char in value):
        return None
    return value


def _unknown_speaker() -> dict[str, str | None]:
    return {"user_key": None, "display_name": "unknown speaker", "username": None}


def _speaker_from_data(value: object) -> dict[str, str | None]:
    if not isinstance(value, Mapping):
        return _unknown_speaker()
    key = _valid_key(value.get("user_key"))
    display = _bounded_string(value.get("display_name"), MAX_DISPLAY_NAME)
    username = _bounded_string(value.get("username"), MAX_USERNAME)
    return {
        "user_key": key,
        "display_name": display if display is not None else "unknown speaker",
        "username": username,
    }


def speaker_snapshot(user: User) -> dict[str, str | None]:
    """Copy the sender's identity as observed on this message, never from members."""
    return {
        "user_key": _valid_key(user.ref.key),
        "display_name": _bounded_string(user.display_name, MAX_DISPLAY_NAME) or "unknown speaker",
        "username": _bounded_string(user.username, MAX_USERNAME),
    }


def _mention_dict(mention: Mention) -> dict[str, Any] | None:
    if not isinstance(mention, Mention):
        return None
    if not isinstance(mention.kind, str):
        return None
    if (
        isinstance(mention.start, bool)
        or isinstance(mention.end, bool)
        or not isinstance(mention.start, int)
        or not isinstance(mention.end, int)
        or mention.start < 0
        or mention.end <= mention.start
        or mention.end > MAX_OFFSET
        or mention.kind not in {"mention", "text_mention"}
        or not isinstance(mention.text, str)
        or not mention.text
        or len(mention.text) > MAX_DISPLAY_NAME
    ):
        return None
    if mention.target is not None and not isinstance(mention.target, UserRef):
        return None
    target_key = _valid_key(mention.target.key) if mention.target is not None else None
    if mention.target is not None and target_key is None:
        return None
    if not isinstance(mention.is_bot, bool):
        return None
    return {
        "start": mention.start,
        "end": mention.end,
        "kind": mention.kind,
        "text": mention.text,
        "target_user_key": target_key,
        "username": _bounded_string(mention.username, MAX_USERNAME),
        "display_name": _bounded_string(mention.display_name, MAX_DISPLAY_NAME),
        "is_bot": mention.is_bot,
    }


def mention_data(mentions: tuple[Mention, ...]) -> list[dict[str, Any]]:
    """Encode a capped sequence of validated mentions as JSON-compatible dictionaries."""
    if not isinstance(mentions, (tuple, list)):
        return []
    result: list[dict[str, Any]] = []
    for mention in mentions:
        item = _mention_dict(mention)
        if item is not None:
            result.append(item)
            if len(result) == MAX_MENTIONS:
                break
    return result


def mentions_from_data(value: object) -> tuple[Mention, ...]:
    """Decode untrusted JSONB mention data, dropping malformed and oversized entries."""
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[Mention] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        target_key = item.get("target_user_key")
        target: UserRef | None = None
        if target_key is not None:
            valid_key = _valid_key(target_key)
            if valid_key is None:
                continue
            try:
                target = UserRef.parse(valid_key)
            except ValueError:
                continue
        start, end = item.get("start"), item.get("end")
        mention = Mention(
            start=start,
            end=end,
            kind=item.get("kind"),
            text=item.get("text"),
            target=target,
            username=item.get("username"),
            display_name=item.get("display_name"),
            is_bot=item.get("is_bot", False),
        )
        normalized = _mention_dict(mention)
        if normalized is not None:
            normalized_target = normalized["target_user_key"]
            result.append(
                Mention(
                    normalized["start"],
                    normalized["end"],
                    normalized["kind"],
                    normalized["text"],
                    UserRef.parse(normalized_target) if normalized_target else None,
                    normalized["username"],
                    normalized["display_name"],
                    normalized["is_bot"],
                )
            )
            if len(result) == MAX_MENTIONS:
                break
    return tuple(result)


def mentions_for_source(mentions: tuple[Mention, ...], source: str) -> tuple[Mention, ...]:
    """Keep only bounded mentions whose stored span still matches the raw source text."""
    if not isinstance(source, str):
        return ()
    return tuple(
        mention
        for mention in mentions_from_data(mention_data(mentions))
        if mention.end <= len(source) and source[mention.start : mention.end] == mention.text
    )


def user_turn_content(message: IncomingMessage, text: str) -> dict[str, Any]:
    """Build the JSONB content for a human turn with original-text provenance."""
    reply_to_id = None
    if message.reply_to is not None and message.reply_to.chat == message.chat:
        reply_to_id = _message_id(message.reply_to.message_id)
    return {
        "text": text,
        "raw_text": message.text,
        "speaker": speaker_snapshot(message.user),
        "mentions": mention_data(mentions_for_source(message.mentions, message.text)),
        "reply_to_id": reply_to_id,
    }


def message_record(row: StoredMessage, *, bot_name: str | None = None) -> dict[str, Any]:
    """Create a safe, stable record from the identity snapshot on a logged row."""
    speaker = (
        {
            "user_key": _valid_key(row.user.key),
            "display_name": (
                f"{_bounded_string(bot_name, MAX_DISPLAY_NAME - 11) or 'Assistant'} (this bot)"
            ),
            "username": None,
        }
        if row.is_bot
        else {
            "user_key": _valid_key(row.user.key),
            "display_name": _bounded_string(row.display_name, MAX_DISPLAY_NAME)
            or "unknown speaker",
            "username": _bounded_string(row.username, MAX_USERNAME),
        }
    )
    record = {
        "message_id": _message_id(row.message_id),
        "role": "assistant" if row.is_bot else "user",
        "speaker": speaker,
        "reply_to_id": _message_id(row.reply_to_id),
        "mentions": mention_data(mentions_for_source(row.mentions, row.text)),
        "text": row.text,
    }
    if row.attachments:
        record["attachments"] = [
            {"index": index, "kind": attachment.kind.value}
            for index, attachment in enumerate(row.attachments)
        ]
    return record


def turn_record(turn: Turn) -> dict[str, Any]:
    """Create an attributed prompt record from a stored conversation turn."""
    content = turn.content if isinstance(turn.content, Mapping) else {}
    role = turn.role if isinstance(turn.role, str) else "unknown"
    raw_text = content.get("raw_text")
    text = raw_text if isinstance(raw_text, str) else content.get("text", "")
    if not isinstance(text, str):
        text = ""
    speaker = _speaker_from_data(content.get("speaker")) if role == "user" else None
    mention_source = content.get("raw_text")
    return {
        "message_id": _message_id(turn.message_id),
        "role": role[:32],
        "speaker": speaker,
        "reply_to_id": _message_id(content.get("reply_to_id")),
        "mentions": mention_data(
            mentions_for_source(mentions_from_data(content.get("mentions")), mention_source)
            if isinstance(mention_source, str)
            else mentions_from_data(content.get("mentions"))
        ),
        "text": text,
    }


def _display_mentions(value: object) -> list[dict[str, Any]]:
    decoded = mentions_from_data(value)
    display: list[dict[str, Any]] = []
    for item in mention_data(decoded):
        display.append(
            {
                "kind": item["kind"],
                "text": item["text"],
                "target_user_key": item["target_user_key"],
                "username": item["username"],
                "display_name": item["display_name"],
                "is_this_bot": item["is_bot"],
            }
        )
    return display


def _normalise_record(record: object) -> dict[str, Any] | None:
    if not isinstance(record, Mapping):
        return None
    role_value = record.get("role")
    role = role_value[:32] if isinstance(role_value, str) else "unknown"
    speaker_value = record.get("speaker")
    speaker = _speaker_from_data(speaker_value) if speaker_value is not None else None
    text = record.get("text")
    if not isinstance(text, str):
        text = ""
    normalized = {
        "message_id": _message_id(record.get("message_id")),
        "role": role,
        "speaker": speaker,
        "reply_to_id": _message_id(record.get("reply_to_id")),
        "mentions": _display_mentions(record.get("mentions")),
        "text": text,
    }
    raw_attachments = record.get("attachments")
    if isinstance(raw_attachments, list):
        allowed_kinds = {kind.value for kind in AttachmentKind}
        attachments = []
        for attachment in raw_attachments[:10]:
            if not isinstance(attachment, Mapping):
                continue
            index = attachment.get("index")
            kind = attachment.get("kind")
            if (
                type(index) is not int
                or index < 0
                or not isinstance(kind, str)
                or kind not in allowed_kinds
            ):
                continue
            attachments.append({"index": index, "kind": kind})
        if attachments:
            normalized["attachments"] = attachments
    return normalized


def _serialize(record: dict[str, Any]) -> str:
    rendered = escape_invisible(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return rendered.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_record(record: dict, *, max_chars: int) -> str:
    """Render one complete, bounded JSON record, never cutting JSON or a wrapper."""
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 2:
        return ""
    normalized = _normalise_record(record)
    if normalized is None:
        return ""
    rendered = _serialize(normalized)
    if len(rendered) <= max_chars:
        return rendered

    mentions = normalized["mentions"]
    original_text = normalized["text"]
    min_prefix = min(256, len(original_text))
    minimum_text = original_text[:min_prefix]
    if min_prefix < len(original_text):
        minimum_text += TRUNCATION_MARKER
    normalized["text"] = minimum_text
    while mentions and len(_serialize(normalized)) > max_chars:
        mentions.pop()

    if len(_serialize(normalized)) > max_chars:
        normalized["text"] = TRUNCATION_MARKER if original_text else ""
        if len(_serialize(normalized)) > max_chars:
            return ""
    if original_text:
        low, high = 0, len(original_text)
        best: str | None = None
        while low <= high:
            middle = (low + high) // 2
            clipped = original_text[:middle] + TRUNCATION_MARKER
            normalized["text"] = clipped
            candidate = _serialize(normalized)
            if len(candidate) <= max_chars:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        if best is not None:
            return best
    else:
        return _serialize(normalized)

    return ""
