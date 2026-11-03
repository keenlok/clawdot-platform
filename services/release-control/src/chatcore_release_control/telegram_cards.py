"""Protected Telegram release-card issuance from immutable ledger bindings."""

from __future__ import annotations

import inspect
import re
import unicodedata
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from .ledger import CardNonce, CardReceipt, LedgerError, ReleaseBinding, ReleaseLedger
from .release_card_view import (
    CardIssueError,
    CardPresentation,
    clean_untrusted,
    computed_flags,
    find_contradictions,
    group_key,
    render_flags,
    touches,
)
from .telegram_bridge import BotApiTransport

MAX_CARD_TEXT_UTF16 = 4096
MAX_UNTRUSTED_SUMMARY = 500
MAX_PATH_DISPLAY = 512
_HEX_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class MessageProtector(Protocol):
    def __call__(self, chat_id: str, message_id: str) -> object: ...


def _single_line(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise CardIssueError("invalid_card")
    clean = "".join(
        " "
        if unicodedata.category(char).startswith("C") or unicodedata.category(char) in {"Zl", "Zp"}
        else char
        for char in value
    )
    return " ".join(clean.split())[:maximum]


def _display_path(change: object) -> str:
    if not isinstance(change, Mapping):
        raise CardIssueError("invalid_card")
    path = _single_line(change.get("path"), maximum=MAX_PATH_DISPLAY)
    change_type = _single_line(change.get("change_type", "modified"), maximum=32)
    if not path or change_type not in {"added", "modified", "deleted", "renamed", "copied"}:
        raise CardIssueError("invalid_card")
    old = change.get("old_path")
    if old is not None:
        old_path = _single_line(old, maximum=MAX_PATH_DISPLAY)
        if not old_path:
            raise CardIssueError("invalid_card")
        path = f"{old_path} → {path}"
    return f"{change_type}: {path}"


def _review_line(findings: int, fixed: int) -> str:
    if findings == 0:
        return "Review: no findings"
    if findings == fixed:
        return f"Review: {findings} findings, {'both fixed' if findings == 2 else 'all fixed'}"
    return f"Review: {findings} findings, {fixed} fixed"


def _render_card(binding, release_kind, inherited, candidate, full_delta, summary, presentation):
    """Plain-language card; every path is cleaned and untrusted PR text is labelled."""
    deployed = binding.deployed_identity
    flags = computed_flags([item["path"] for item in full_delta])
    clean_title = clean_untrusted(presentation.pr_title, maximum=100) if presentation else ""
    header = [
        f"Release request: PR #{binding.pr_number}" + (f' "{clean_title}"' * bool(clean_title))
    ]
    if presentation:
        header.append(presentation.pr_url)
    if release_kind == "reconciliation":
        header.append(
            "⚠️ Reconciliation: includes previously undeployed changes "
            f"({len(inherited)} inherited D→B files + {len(candidate)} PR B→H files)"
        )

    if presentation:
        changes = [clean_untrusted(line, maximum=160) for line in presentation.what_changes]
        risks = [clean_untrusted(line, maximum=160) for line in presentation.risks]
    else:
        changes, risks = ([clean_untrusted(summary, maximum=500)] if summary else []), []
    warnings = dict(find_contradictions([clean_title, *changes, *risks], flags))
    change_lines = [warnings.get(1 + i, f"- {line}") for i, line in enumerate(changes)]
    if 0 in warnings:
        change_lines.insert(0, warnings[0])
    risk_lines = [warnings.get(1 + len(changes) + i, f"- {line}") for i, line in enumerate(risks)]
    changes_heading = "What changes (from the PR description)" if presentation else "What changes"

    stats = presentation.file_stats if presentation else ()
    if stats:
        groups = touches(stats)
    else:
        counts: dict[str, int] = {}
        for change in full_delta:
            counts[group_key(change["path"])] = counts.get(group_key(change["path"]), 0) + 1
        groups = [
            {"key": key, "files": count}
            for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    def group_line(group) -> str:
        key = _single_line(group["key"], maximum=MAX_PATH_DISPLAY)
        if not stats:
            return f"- {key} ({group['files']} files)"
        if group["files"] == 1:
            stat = next(s for s in stats if s.path in group["paths"])
            key = _single_line(stat.path, maximum=MAX_PATH_DISPLAY)
            counts = "binary" if stat.added is None else f"+{stat.added} / −{stat.deleted}"
            return f"- {key} ({counts})"
        if group["has_binary"] and not group["added"] and not group["deleted"]:
            return f"- {key} (binary, {group['files']} files)"
        return f"- {key} (+{group['added']} / −{group['deleted']}, {group['files']} files)"

    checks = presentation.checks if presentation else ()
    symbols = {"success": "✅", "failure": "❌"}
    if checks:
        check_lines = [
            "",
            "Checks",
            "  ".join(
                f"{symbols.get(c.conclusion, '⚪')} {c.name}" + (f" {c.detail}" if c.detail else "")
                for c in checks
            ),
        ]
    else:
        check_lines = ["", "Checks: trusted evidence bound (see Details)"]
    if presentation and presentation.review_findings is not None:
        check_lines.append(_review_line(presentation.review_findings, presentation.review_fixed))
    footer = [
        f"Rollback: previous image kept ({deployed.image_digest[:19]})"
        + (
            "; ⚠️ includes a DB migration — image rollback won't undo it"
            if flags["migrations"]
            else "; no DB change to undo"
        ),
        f"Approving merges and deploys {binding.head_sha[:7]} → {binding.target}",
    ]

    def compose(shown_groups: int, shown_changes: int, shown_risks: int) -> str:
        lines = list(header)
        if change_lines:
            lines += ["", changes_heading, *change_lines[:shown_changes]]
            if shown_changes < len(change_lines):
                lines.append("- …")
        touch_lines = [group_line(g) for g in groups[:shown_groups]]
        if shown_groups < len(groups):
            rest = sum(g["files"] for g in groups[shown_groups:])
            touch_lines.append(f"- and {rest} more files (full list in View diff / Details)")
        lines += ["", "Touches", *touch_lines, render_flags(flags), *check_lines]
        if risk_lines:
            lines += ["", "Risk / limits (from the PR description)", *risk_lines[:shown_risks]]
            if shown_risks < len(risk_lines):
                lines.append("- …")
        return "\n".join([*lines, *footer])

    def fits(text: str) -> bool:
        return len(text.encode("utf-16-le")) // 2 <= MAX_CARD_TEXT_UTF16

    # Long path lists collapse first (phones), then untrusted bullets, risks before changes.
    shown_groups = len(groups) if len(groups) <= 7 else 6
    while shown_groups > 1 and not fits(compose(shown_groups, len(change_lines), len(risk_lines))):
        shown_groups -= 1
    shown_changes, shown_risks = len(change_lines), len(risk_lines)
    while shown_risks and not fits(compose(shown_groups, shown_changes, shown_risks)):
        shown_risks -= 1
    while shown_changes and not fits(compose(shown_groups, shown_changes, shown_risks)):
        shown_changes -= 1
    value = compose(shown_groups, shown_changes, shown_risks)
    if not fits(value):
        raise CardIssueError("invalid_card")
    return value


def _text(
    binding: ReleaseBinding, summary: str, presentation: CardPresentation | None = None
) -> str:
    review = binding.review_bundle
    deployed = binding.deployed_identity
    try:
        release_kind = review.get("release_kind", "ordinary")
        if (
            type(release_kind) is not str
            or release_kind not in {"ordinary", "reconciliation"}
            or review.get("complete_delta_reviewed") is not True
            or review.get("base_is_ancestor_of_head") is not True
            or review.get("deployed_commit") != deployed.git_sha
            or review.get("base_commit") != binding.base_sha
            or review.get("head_commit") != binding.head_sha
            or review.get("head_tree") != binding.head_tree
            or review.get("policy_digest") != binding.policy_digest
            or not isinstance(review.get("check_evidence_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", review["check_evidence_digest"]) is None
            or any(
                not isinstance(review.get(key), str) or not _HEX_SHA.fullmatch(review[key])
                for key in ("deployed_tree", "base_tree", "head_tree")
            )
        ):
            raise CardIssueError("invalid_card")
        if (
            type(review.get("base_is_descendant_of_deployed")) is not bool
            or type(review.get("same_tree_continuation_recorded")) is not bool
        ):
            raise CardIssueError("invalid_card")
        if release_kind == "ordinary":
            if review["base_commit"] == deployed.git_sha:
                if review["base_tree"] != review["deployed_tree"]:
                    raise CardIssueError("invalid_card")
            elif not (
                review["base_is_descendant_of_deployed"] is True
                and review["same_tree_continuation_recorded"] is True
                and review["base_tree"] == review["deployed_tree"]
            ):
                raise CardIssueError("invalid_card")
        inherited = review["deployed_to_base"]
        candidate = review["base_to_head"]
        full_delta = review["deployed_to_head"]
        if (
            not isinstance(inherited, list)
            or not isinstance(candidate, list)
            or not isinstance(full_delta, list)
        ):
            raise CardIssueError("invalid_card")
        for change in (*inherited, *candidate, *full_delta):
            _display_path(change)
        value = _render_card(
            binding, release_kind, inherited, candidate, full_delta, summary, presentation
        )
        if len(value.encode("utf-16-le")) // 2 > MAX_CARD_TEXT_UTF16:
            raise CardIssueError("invalid_card")
        return value
    except CardIssueError:
        raise
    except Exception:
        raise CardIssueError("invalid_card") from None


class ReleaseCardIssuer:
    """Send a plain, protected approval card, then register its confirmed message."""

    def __init__(
        self,
        *,
        ledger: ReleaseLedger,
        transport: BotApiTransport,
        token: str,
        current_telegram_owner: Callable[[], int],
        protect_message: MessageProtector,
    ) -> None:
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 512
            or not callable(current_telegram_owner)
            or not callable(protect_message)
        ):
            raise CardIssueError("invalid_card")
        self._ledger = ledger
        self._transport = transport
        self._token = token
        self._current_telegram_owner = current_telegram_owner
        self._protect_message = protect_message

    async def issue(
        self,
        *,
        binding: ReleaseBinding,
        owner_user_id: int,
        summary: str = "",
        presentation: CardPresentation | None = None,
        now: datetime | None = None,
    ) -> CardReceipt:
        if (
            not isinstance(binding, ReleaseBinding)
            or type(owner_user_id) is not int
            or owner_user_id <= 0
        ):
            raise CardIssueError("invalid_card")
        try:
            current_owner = self._current_telegram_owner()
        except Exception:
            raise CardIssueError("invalid_card") from None
        if type(current_owner) is not int or current_owner <= 0 or owner_user_id != current_owner:
            raise CardIssueError("invalid_card")
        summary_line = _single_line(summary, maximum=MAX_UNTRUSTED_SUMMARY)
        if presentation is not None and not isinstance(presentation, CardPresentation):
            raise CardIssueError("invalid_card")
        card_text = _text(binding, summary_line, presentation)
        nonce = CardNonce()
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"release:a:{nonce.value}"},
                    {"text": "Reject", "callback_data": f"release:r:{nonce.value}"},
                ],
                [
                    {"text": "Details", "callback_data": "release:i"},
                    {"text": "View diff", "callback_data": "release:v"},
                ],
            ]
        }
        try:
            sent = await self._transport.call(
                "sendMessage",
                {
                    "chat_id": owner_user_id,
                    "text": card_text,
                    "link_preview_options": {"is_disabled": True},
                    "reply_markup": markup,
                },
                token=self._token,
            )
        except Exception:
            raise CardIssueError("send_uncertain") from None
        if not isinstance(sent, Mapping):
            raise CardIssueError("send_uncertain")
        chat = sent.get("chat")
        raw_chat_id = chat.get("id") if isinstance(chat, Mapping) else None
        message_id = sent.get("message_id")
        if (
            type(raw_chat_id) is not int
            or raw_chat_id != owner_user_id
            or type(message_id) is not int
            or message_id <= 0
        ):
            raise CardIssueError("send_uncertain")
        try:
            protected = self._protect_message(str(owner_user_id), str(message_id))
            if inspect.isawaitable(protected):
                await protected
        except Exception:
            raise CardIssueError("protection_unavailable") from None
        try:
            return await self._ledger.create_card(
                binding=binding,
                owner_user_id=owner_user_id,
                chat_id=owner_user_id,
                message_id=message_id,
                nonce=nonce,
                now=(now or datetime.now(UTC)),
            )
        except LedgerError:
            raise CardIssueError("ledger_unavailable") from None
        except Exception:
            raise CardIssueError("ledger_unavailable") from None

    def __repr__(self) -> str:
        return "ReleaseCardIssuer(token=[REDACTED])"


__all__ = ["CardIssueError", "ReleaseCardIssuer"]
