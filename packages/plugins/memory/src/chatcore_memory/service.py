"""Memory presentation and text normalization helpers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, Literal

from chatcore.events import UserRef

DROP_FACT: Literal["drop"] = "drop"


def resolve_fact_subject(
    fact: Mapping[str, Any], labels: Mapping[str, UserRef], speaker: UserRef
) -> UserRef | None | Literal["drop"]:
    about_speaker = fact.get("about_speaker") is True
    about_other = fact.get("about_other") is True
    label = fact.get("about_label")
    # Models commonly send an explicit null for an unused optional field.
    label_present = label not in (None, "")
    valid_label = isinstance(label, str) and label in labels

    if (about_other and about_speaker) or (label_present and not valid_label):
        return DROP_FACT
    if about_speaker and valid_label:
        return DROP_FACT
    if about_other and not valid_label:
        return DROP_FACT
    if valid_label:
        return labels[label]
    if about_speaker:
        return speaker
    return None


def safe_name(value: str | None) -> str:
    if not value:
        return "unknown person"
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        char
        for char in normalized
        if char not in "[]()"
        and char not in "\r\n"
        and not unicodedata.category(char).startswith("C")
    )
    normalized = " ".join(normalized.split())[:40].strip()
    return normalized or "unknown person"


def _saved_date(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC).date().isoformat()
    except (TypeError, ValueError):
        return "unknown date"


def render_fact(row: Mapping[str, Any], names: Mapping[str, str]) -> str:
    subject = row.get("subject_user_key")
    creator = row.get("created_by")
    source = row.get("source")
    if subject:
        attribution = f"about {safe_name(names.get(str(subject)))}"
    elif source == "auto":
        attribution = f"from {safe_name(names.get(str(creator)))}"
    else:
        attribution = f"saved by {safe_name(names.get(str(creator)))}"
    parts = [attribution, f"saved {_saved_date(row.get('created_at'))}"]
    created = row.get("created_at")
    updated = row.get("updated_at")
    if source == "auto" and isinstance(created, datetime) and isinstance(updated, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        if updated > created:
            parts.append("unverified")
    return f"#{row['id']} [{'; '.join(parts)}] {row['text']}"


def build_context(
    pinned: Sequence[Mapping[str, Any]],
    recalled: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
    *,
    max_chars: int = 5000,
) -> str:
    if max_chars <= 0:
        return ""
    chosen: list[tuple[str, list[str]]] = []
    seen: set[int] = set()
    for heading, rows in (
        ("Pinned facts about this chat", pinned),
        ("Possibly relevant memories", recalled),
    ):
        lines: list[str] = []
        for row in rows:
            memory_id = int(row["id"])
            if memory_id in seen:
                continue
            line = render_fact(row, names)
            proposed = [*lines, line]
            sections = [*chosen, (heading, proposed)]
            whole = "\n\n".join(f"{title}\n" + "\n".join(items) for title, items in sections)
            if len(whole) > max_chars:
                admitted = [*chosen]
                if lines:
                    admitted.append((heading, lines))
                return "\n\n".join(f"{title}\n" + "\n".join(items) for title, items in admitted)
            lines.append(line)
            seen.add(memory_id)
        if lines:
            chosen.append((heading, lines))
    return "\n\n".join(f"{heading}\n" + "\n".join(lines) for heading, lines in chosen)


def normalize_fact(text: str) -> str:
    value = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return re.sub(r"[\s.,!?;:。！？、，；：]+$", "", value)


def jaccard_tokens(text: str) -> set[str]:
    tokens = re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())
    result: set[str] = set()
    for token in tokens:
        run = ""
        for char in token:
            is_cjk = any(
                script in unicodedata.name(char, "")
                for script in ("CJK", "HIRAGANA", "KATAKANA", "HANGUL")
            )
            if is_cjk:
                if run:
                    result.add(run)
                    run = ""
                result.add(char)
            else:
                run += char
        if run:
            result.add(run)
    return result


def jaccard(left: str, right: str) -> float:
    a, b = jaccard_tokens(left), jaccard_tokens(right)
    return len(a & b) / len(a | b) if a or b else 1.0
