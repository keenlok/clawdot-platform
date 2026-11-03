"""Pure, bounded result formatting; platform totals win over locally observed votes."""

from collections.abc import Mapping, Sequence
from typing import Any

from chatcore_polls.models import Poll


def _length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _clip(text: str, limit: int) -> str:
    if _length(text) <= limit:
        return text
    return (
        text.encode("utf-16-le")[: max(0, limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"
    )


def format_results(
    poll: Poll,
    counts: tuple[int, ...] | None,
    votes: Sequence[Mapping[str, Any]],
    *,
    max_length: int = 4096,
) -> str:
    header = f"📊 Poll #{poll.number} closed: {poll.question}"
    if poll.multiple:
        header += " (voters could pick several)"
    if counts is None and poll.anonymous:
        return _clip(header + "\nFinal results are unavailable.", max_length)
    if counts is None:
        counts = tuple(sum(v["option_index"] == i for v in votes) for i in range(len(poll.options)))
    lines = [
        header,
        *(f"{option} — {count}" for option, count in zip(poll.options, counts, strict=True)),
    ]
    highest = max(counts, default=0)
    winners = [
        option for option, count in zip(poll.options, counts, strict=True) if count == highest
    ]
    if not highest:
        lines.append("No votes.")
    elif len(winners) == 1:
        lines.append(f"Winner: {winners[0]} ({highest} {'vote' if highest == 1 else 'votes'}).")
    else:
        names = ", ".join(winners[:-1]) + " and " + winners[-1]
        summary = f"Tie: {names} ({highest} {'vote' if highest == 1 else 'votes'} each)."
        # Long Unicode labels can consume the message limit twice in a large tie.
        # Keep full option/count lines and refer to their positions in that case.
        voter_space = 30 * len(poll.options) if votes and not poll.anonymous else 0
        if _length("\n".join([*lines, summary])) + voter_space > max_length:
            positions = [str(i + 1) for i, count in enumerate(counts) if count == highest]
            names = "options " + ", ".join(positions[:-1]) + " and " + positions[-1]
            summary = f"Tie: {names} ({highest} {'vote' if highest == 1 else 'votes'} each)."
        lines.append(summary)
    base = "\n".join(lines)
    if poll.anonymous or not votes:
        return _clip(base, max_length)
    groups = [
        (i, option, [v["voter_name"] for v in votes if v["option_index"] == i])
        for i, option in enumerate(poll.options)
    ]
    groups = [(i, option, names) for i, option, names in groups if names]
    available = max_length - _length(base + "\nVoters — ")
    parts = []
    for index, (option_index, option, names) in enumerate(groups):
        remaining_groups = len(groups) - index
        budget = max(0, (available - 2 * (remaining_groups - 1)) // remaining_groups)
        prefix = f"{option}: "
        if _length(prefix + f"+{len(names)} more") > budget:
            prefix = f"Option {option_index + 1}: "
        shown = []
        for name_index, name in enumerate(names):
            remaining = len(names) - name_index - 1
            proposed = [*shown, name]
            suffix = [f"+{remaining} more"] if remaining else []
            if _length(prefix + ", ".join([*proposed, *suffix])) > budget:
                break
            shown.append(name)
        hidden = len(names) - len(shown)
        label = prefix + ", ".join([*shown, *([f"+{hidden} more"] if hidden else [])])
        parts.append(label)
        available -= _length(label) + 2
    return _clip(base + ("\nVoters — " + "; ".join(parts) if parts else ""), max_length)
