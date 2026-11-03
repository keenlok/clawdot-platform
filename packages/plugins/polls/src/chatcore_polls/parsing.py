"""Command-only syntax sugar; all output still passes the shared draft validator."""

import re
from datetime import date, timedelta
from typing import Any

USAGE = (
    "Use /poll [--multi|--single] [--anon] [--close <ISO local time> <IANA zone>] "
    "Question? | A | B."
)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and (text[0], text[-1]) in {('"', '"'), ("“", "”")}:
        return text[1:-1].strip()
    return text


def parse_poll(text: str, *, max_options: int) -> dict[str, Any]:
    remaining = text.strip()
    seen = set()
    data: dict[str, Any] = {"anonymous": False}
    while remaining.startswith("--"):
        flag, *rest = remaining.split(maxsplit=1)
        remaining = rest[0] if rest else ""
        kind = "choice" if flag in {"--single", "--multi"} else flag
        if flag not in {"--single", "--multi", "--anon", "--close"} or kind in seen:
            raise ValueError(USAGE)
        seen.add(kind)
        if kind == "choice":
            data["multiple"] = flag == "--multi"
        elif flag == "--anon":
            data["anonymous"] = True
        else:
            parts = remaining.split(maxsplit=2)
            if len(parts) != 3:
                raise ValueError(USAGE)
            data["close_local_time"], data["close_timezone"], remaining = parts
    parts = remaining.split(maxsplit=2)
    if (
        len(parts) >= 2
        and parts[0] == "dates"
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}", parts[1])
    ):
        try:
            start, end = (date.fromisoformat(v) for v in parts[1].split(".."))
        except ValueError:
            raise ValueError(USAGE) from None
        length = (end - start).days + 1
        if length < 1:
            raise ValueError(USAGE)
        if length > max_options:
            raise ValueError(
                f"That range has {length} dates; the maximum is {max_options} options."
            )
        labels = []
        for i in range(length):
            day = start + timedelta(days=i)
            label = f"{day:%a} {day.day} {day:%b}"
            if start.year != end.year:
                label += f" {day.year}"
            labels.append(label)
        data.update(
            question=_unquote(parts[2]) if len(parts) == 3 else "Which dates work?", options=labels
        )
        data.setdefault("multiple", True)
    else:
        parts = remaining.split("|")
        if len(parts) < 2:
            raise ValueError(USAGE)
        data.update(question=_unquote(parts[0]), options=parts[1:])
        data.setdefault("multiple", False)
    return data
