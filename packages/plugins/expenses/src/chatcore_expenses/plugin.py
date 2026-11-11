"""Splitwise-like expense tools with requester-confirmed writes."""

from __future__ import annotations

import html
import logging
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from chatcore.cards import CardError, CardRenderClient, can_deliver_cards, send_card_pages
from chatcore.events import ChatRef, User, UserRef
from chatcore.files import FilePlatform
from chatcore.images import ImageUnavailable, is_image
from chatcore.limits import Limits
from chatcore.pending import PendingAction, PendingActionHandler
from chatcore.platform import PlatformError
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.skills import Skill
from chatcore.store.base import Stores
from chatcore.tools.registry import Tool, ToolContext, ToolResult

from .balances import ledger_balances, simplify_balances
from .cards import balance_card, trip_summary_card
from .currency import (
    currency_exponent,
    currency_for_country,
    format_amount,
    parse_amount,
    resolve_currency,
)
from .export import build_export, export_caption
from .fx import get_rate
from .gates import build_gates
from .receipt import ReceiptError, ReceiptReader, ReceiptSettings
from .repository import (
    LedgerRepo,
    PostgresLedgerRepo,
    _apply_trip_changes,
    _check_trip_change_usage,
    _expense_snapshot,
    _trip_change_lines,
)
from .split import split_amount

logger = logging.getLogger(__name__)
GroupResolver = Callable[[ToolContext], Awaitable[ChatRef | None]]
MEMBER_NAME_DESCRIPTION = (
    "Name as the user wrote it (display name, @username, or me). Don't convert a name into an "
    "@username the user didn't type."
)


def _member_labels(members: Iterable[User], keys: Iterable[str] | None = None) -> dict[str, str]:
    """Display names by user key, with ``(@username)`` for similar names among ``keys``.

    Two listed members whose display names share a case-insensitive prefix of 3+
    characters are told apart by username; unrelated names stay unchanged.
    """
    by_key = {member.ref.key: member for member in members}
    listed = (
        by_key
        if keys is None
        else {key: by_key[key] for key in dict.fromkeys(keys) if key in by_key}
    )

    def prefix(member: User) -> str:
        return member.display_name.strip().casefold()[:3]

    counts = Counter(prefix(m) for m in listed.values() if len(prefix(m)) == 3)
    labels = {key: member.display_name for key, member in by_key.items()}
    for key, member in listed.items():
        if member.username and counts[prefix(member)] > 1:
            labels[key] = f"{member.display_name} (@{member.username})"
    return labels


def _member_names(keys, labels: dict[str, str]) -> str:
    names = [labels.get(key, key) for key in keys]
    if len(names) > 8:
        names = [*names[:8], f"+{len(names) - 8} more"]
    return ", ".join(names)


def _shares_text(expense: dict, labels: dict[str, str]) -> str:
    """Who shares an expense: names for an equal split, per-person amounts otherwise."""
    shares = {share["user_key"]: int(share["amount"]) for share in expense["shares"]}
    if not shares:
        return "split: none"
    if max(shares.values()) - min(shares.values()) <= 1:
        return "split: " + _member_names(shares, labels)
    items = [
        f"{labels.get(key, key)} {format_amount(value, expense['currency'])}"
        for key, value in shares.items()
    ]
    if len(items) > 8:
        items = [*items[:8], f"+{len(items) - 8} more"]
    return "split: " + ", ".join(items)


def _split_change_text(
    before: dict[str, int], after: dict[str, int], labels: dict[str, str]
) -> str:
    before_keys = list(before)
    after_keys = list(after)
    before_set = set(before_keys)
    after_set = set(after_keys)
    added = [key for key in after_keys if key not in before_set]
    removed = [key for key in before_keys if key not in after_set]
    if added or removed:
        changes = []
        if added:
            changes.append(f"added {_member_names(added, labels)}")
        if removed:
            changes.append(f"removed {_member_names(removed, labels)}")
        text = "split: " + "; ".join(changes)
        if len(before_keys) != len(after_keys):
            text += f" ({len(before_keys)} ways → {len(after_keys)} ways)"
        return text
    return f"split amounts changed ({_member_names(after_keys, labels)})"


# Default behaviour text for the `expenses` skill; apps may add to or override it.
EXPENSES_INSTRUCTIONS = (
    "Expenses default to SGD. Writes apply immediately with an echo; don't repeat it or ask "
    "to save. Undo: undo_expense_change. Ask only for missing details. Omit split_with to "
    "split equally among trip members, payer included. Use the current trip unless one "
    "is named; general=true is the general ledger. Receipts: read_receipt, then "
    "add_expense with its total; if it fails, read a visible image yourself. Payer defaults "
    "to sender; mention warnings; no item-level splits. export_expenses: whole "
    "ledger. Pass people's names as written; never swap in a guessed @username. Members "
    "with similar names are different people; never suggest merging them. Answer who shares "
    "an expense from list_expenses, not earlier replies. To explain one person's balance or "
    "history, call member_expenses; never guess shares."
)

# Default behaviour text for the `trips` skill; apps may add to or override it.
TRIPS_INSTRUCTIONS = (
    "Several trips can be planned or active. start_trip never replaces another; future "
    "trips use status: planned or a future start_date; start a new-location trip active. "
    "Use list_trips if unsure; ask if ambiguous. End only the confirmed trip when asked; "
    "reopen_trip undoes accidental end. update_trip changes a named planned/active trip "
    "immediately with an echo. Omit split_with to split equally among members, payer "
    "included; start_trip.members sets the initial list. settle_up, trip lifecycle changes "
    "and FX overrides need confirmation. Adding a country adds currency; settle "
    "mode/currency change only before any expense. In convert trips, add_expense fetches "
    "the expense-date rate automatically; set_fx_rate is optional. Rates use Frankfurter; "
    "open.er-api is an approximate fallback."
)


def _plain_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Text values must be strings.")
    for _ in range(2):
        if not re.search(r"&(?:#\d+|#x[\da-fA-F]+|[A-Za-z][A-Za-z0-9]+);", value):
            break
        value = html.unescape(value)
    return value.strip()


async def _resolve_trip(ledger: LedgerRepo, chat: ChatRef, ref: str) -> dict:
    ref = _plain_text(ref)
    if not ref:
        raise ValueError("Say which trip.")
    trips = await ledger.trips(chat)
    try:
        full_id = str(UUID(ref))
    except ValueError:
        full_id = None
    if full_id:
        by_id = await ledger.trip(chat, full_id)
        if by_id:
            return by_id
    if re.fullmatch(r"[0-9a-fA-F]{8}", ref):
        prefixes = [
            trip for trip in trips if trip["id"].replace("-", "").lower().startswith(ref.lower())
        ]
        if len(prefixes) == 1:
            return prefixes[0]
        if len(prefixes) > 1:
            raise ValueError(f"Trip ID prefix {ref} matches several trips.")
    exact = [trip for trip in trips if trip["name"].casefold() == ref.casefold()]
    if exact:
        opened = [trip for trip in exact if trip["status"] != "closed"]
        return opened[0] if opened else max(exact, key=lambda trip: trip["started_at"])
    matches = [trip for trip in trips if ref.casefold() in trip["name"].casefold()]
    open_matches = [trip for trip in matches if trip["status"] != "closed"]
    if len(open_matches) == 1:
        return open_matches[0]
    if not open_matches and len(matches) == 1:
        return matches[0]
    candidates = ", ".join(trip["name"] for trip in (open_matches or matches)[:5])
    if candidates:
        raise ValueError(f"{ref!r} matches several trips: {candidates}. Which one?")
    names = ", ".join(trip["name"] for trip in trips[:5]) or "none"
    raise ValueError(f'Trip "{ref}" not found. Trips: {names}')


async def _current_trip(ledger: LedgerRepo, chat: ChatRef) -> dict | None:
    default_id = await ledger.default_trip_id(chat)
    if default_id:
        return await ledger.trip(chat, default_id)
    active = await ledger.trips(chat, statuses=("active",))
    if len(active) == 1:
        return active[0]
    if not active:
        return None
    names = ", ".join(trip["name"] for trip in active[:5])
    raise ValueError(
        f"Several trips are active ({names}) and none is the default. Which trip is this for? "
        "You can also set a default with set_default_trip."
    )


class ExpensesPlugin(BasePlugin):
    name = "expenses"
    explainer_package = "chatcore_expenses"
    help_title = "Expenses and trips"
    migrations_package = "chatcore_expenses.migrations"

    def skills(self):
        examples = (
            "add this expense",
            "split the bill",
            "记下这笔账",
            "查一下余额",
            "帮我结账",
            "split among us can?",
            "edit that expense amount",
            "改一下这笔账，undo ok?",
        )
        return [
            Skill(
                "expenses",
                "Track shared expenses and balances",
                examples,
                (
                    "add_expense",
                    "delete_expense",
                    "list_expenses",
                    "get_balances",
                    "member_expenses",
                    "settle_up",
                    "export_expenses",
                    "read_receipt",
                    "get_trip",
                    "update_expense",
                    "restore_expense",
                    "undo_expense_change",
                )
                + (("send_balance_card", "send_trip_summary_card") if self.cards else ()),
                keywords=("expense", "balance", "费用", "账单"),
                max_steps=5,
                instructions=EXPENSES_INSTRUCTIONS,
            ),
            Skill(
                "trips",
                "Manage group trips and trip balances",
                (
                    "start a trip",
                    "update the trip",
                    "开始旅行账本",
                    "结束这次旅行",
                    "trip expenses how",
                    "调一下汇率",
                    "this trip split how ah",
                    "switch to the other trip can?",
                ),
                (
                    "start_trip",
                    "update_trip",
                    "end_trip",
                    "get_trip",
                    "set_fx_rate",
                    "get_balances",
                    "list_trips",
                    "activate_trip",
                    "reopen_trip",
                    "set_default_trip",
                    "update_trip_members",
                )
                + (("send_balance_card", "send_trip_summary_card") if self.cards else ()),
                keywords=("trip", "travel", "旅行", "旅费"),
                max_steps=4,
                instructions=TRIPS_INSTRUCTIONS,
            ),
        ]

    def tool_gates(self):
        return build_gates(self)

    def __init__(
        self,
        *,
        group_for_dm: GroupResolver | None = None,
        ledger: LedgerRepo | None = None,
        timezone: str = "Asia/Singapore",
        receipt_reader: ReceiptReader | None = None,
        receipt_settings: ReceiptSettings | None = None,
        limits: Limits | None = None,
        cards: CardRenderClient | None = None,
        cards_theme: str = "light",
    ) -> None:
        self.group_for_dm = group_for_dm
        self.ledger = ledger
        self.timezone = ZoneInfo(timezone)
        self.receipt_reader = receipt_reader
        self.receipt_settings = receipt_settings or ReceiptSettings()
        self.limits = limits
        self.cards = cards
        self.cards_theme = cards_theme

    def _ledger(self, stores: Stores) -> LedgerRepo:
        if self.ledger is not None:
            return self.ledger
        if stores.db is None:
            raise ValueError("Expense ledger storage is not configured.")
        return PostgresLedgerRepo(stores.db)

    async def _chat(self, ctx: ToolContext) -> ChatRef | None:
        if not ctx.is_dm:
            return ctx.chat
        if self.group_for_dm is None:
            return None
        return await self.group_for_dm(ctx)

    async def resolve_trip(self, ctx: ToolContext, trip: str) -> dict:
        """Resolve an explicit, existing trip for app-injected integrations.

        This returns identity metadata only, never a ledger export. Recheck current
        group membership even when a DM group resolver returned a cached group.
        """
        if not isinstance(trip, str) or not trip.strip():
            raise ValueError("Say which trip; provide an existing trip name or ID.")
        chat = await self._chat(ctx)
        if chat is None:
            raise ValueError("I can't identify your group from this DM. Use the group chat.")
        if not any(member.ref == ctx.user.ref for member in await self._members(ctx, chat)):
            raise PermissionError("Trip files require current membership in the trip's group.")
        selected = await _resolve_trip(self._ledger(ctx.stores), chat, trip)
        return {
            "chat_key": chat.key,
            "trip_id": str(selected["id"]),
            "trip_name": str(selected["name"]),
            "status": str(selected["status"]),
        }

    async def _members(self, ctx: ToolContext, chat: ChatRef) -> list[User]:
        return await ctx.stores.repo.find_members(chat)

    async def _resolve_member(self, ctx: ToolContext, chat: ChatRef, name: str) -> User:
        name = _plain_text(name)
        normalized = name.lstrip("@").strip().casefold()
        if normalized in {"me", "i"}:
            return ctx.user
        members = await self._members(ctx, chat)
        matches = [
            member
            for member in members
            if (member.username and member.username.casefold().startswith(normalized))
            or member.display_name.casefold().startswith(normalized)
        ]
        exact = [
            m
            for m in matches
            if (m.username and m.username.casefold() == normalized)
            or m.display_name.casefold() == normalized
        ]
        matches = exact or matches
        unique = {member.ref.key: member for member in matches}
        if len(unique) == 1:
            return next(iter(unique.values()))
        candidates = ", ".join(sorted(m.display_name for m in unique.values())) or "none"
        message = "Ambiguous" if unique else "Unknown"
        raise ValueError(f"{message} member {name!r}. Candidates: {candidates}.")

    def _pending(
        self, ctx: ToolContext, chat: ChatRef, kind: str, payload: dict, summary: str
    ) -> ToolResult:
        action = PendingAction.create(
            chat=chat,
            requester=ctx.user.ref,
            plugin=self.name,
            kind=kind,
            payload=payload,
            summary=summary,
            thread_id=ctx.thread_id,
        )
        return ToolResult(content=summary, pending_action=action)

    async def _add_expense(
        self,
        ctx: ToolContext,
        *,
        description: str,
        amount: str,
        paid_by: str,
        split_with: list[str] | None = None,
        split_type: str = "equal",
        currency: str | None = None,
        shares: dict[str, Any] | None = None,
        spent_on: str | None = None,
        trip: str | None = None,
        general: bool = False,
    ) -> ToolResult:
        return await self._add_expense_impl(
            ctx,
            description=description,
            amount=amount,
            paid_by=paid_by,
            split_with=split_with,
            split_type=split_type,
            currency=currency,
            shares=shares,
            spent_on=spent_on,
            trip=trip,
            general=general,
        )

    async def _add_expense_impl(
        self,
        ctx: ToolContext,
        *,
        description: str,
        amount: str,
        paid_by: str,
        split_with: list[str] | None = None,
        split_type: str = "equal",
        currency: str | None = None,
        shares: dict[str, Any] | None = None,
        spent_on: str | None = None,
        trip: str | None = None,
        general: bool = False,
        _reuse_fx_rate: bool = False,
        _stored_fx_rate: str | None = None,
        _resolved_payer_key: str | None = None,
        _resolved_participant_keys: list[str] | None = None,
        _resolved_share_values: dict[str, Any] | None = None,
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        try:
            ledger = self._ledger(ctx.stores)
            if not isinstance(general, bool):
                raise ValueError("general must be a boolean.")
            if trip is not None and general:
                raise ValueError("Choose a trip or the general ledger, not both.")
            description = _plain_text(description)
            if not description:
                raise ValueError("Description cannot be empty.")
            selected = None
            if trip is not None:
                selected = await _resolve_trip(ledger, chat, trip)
                if selected["status"] == "closed":
                    raise ValueError("The trip is closed. Reopen it first.")
            elif not general:
                selected = await _current_trip(ledger, chat)
            currency = resolve_currency(
                currency, selected["currencies"] if selected else None, trip_active=bool(selected)
            )
            amount_minor = parse_amount(amount, currency)
            if _resolved_payer_key is not None:
                known = {item.ref.key: item for item in await self._members(ctx, chat)}
                payer = known.get(
                    _resolved_payer_key,
                    User(UserRef.parse(_resolved_payer_key), _resolved_payer_key),
                )
            else:
                payer = await self._resolve_member(ctx, chat, paid_by)
            if _resolved_participant_keys is not None:
                known = {item.ref.key: item for item in await self._members(ctx, chat)}
                participants = [
                    known.get(key, User(UserRef.parse(key), key))
                    for key in _resolved_participant_keys
                ]
            elif split_with:
                participants = [await self._resolve_member(ctx, chat, name) for name in split_with]
            elif selected and selected.get("members"):
                members = await self._members(ctx, chat)
                member_by_key = {item.ref.key: item for item in members}
                participants = [
                    member_by_key[key] for key in selected["members"] if key in member_by_key
                ]
                if payer.ref.key not in {item.ref.key for item in participants}:
                    raise ValueError(
                        f"{payer.display_name} isn't on the {selected['name']}'s member list. "
                        "Add them with update_trip_members, or say who shares this."
                    )
            else:
                raise ValueError(
                    "Who shares this? (Tip: set trip members with update_trip_members so I can "
                    "split among everyone.)"
                )
            participant_map = {user.ref.key: user for user in participants}
            participant_keys = list(participant_map)
            split_values = None
            if _resolved_share_values is not None:
                split_values = _resolved_share_values
            elif shares is not None:
                split_values = {}
                for name, value in shares.items():
                    member = await self._resolve_member(ctx, chat, name)
                    if split_type == "exact":
                        split_values[member.ref.key] = parse_amount(str(value), currency)
                    else:
                        split_values[member.ref.key] = str(value)
                if set(split_values) != set(participant_keys):
                    raise ValueError("Share entries must match the split participants.")
            share_amounts = split_amount(
                amount_minor,
                participant_keys,
                payer.ref.key,
                split_type,
                split_values,
                currency=currency,
            )
            today = datetime.now(self.timezone).date()
            if spent_on is None:
                spent = today
            else:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", spent_on):
                    raise ValueError("spent_on must use YYYY-MM-DD.")
                spent = date.fromisoformat(spent_on)
                if spent > today:
                    raise ValueError("spent_on cannot be in the future.")
            rate = None
            approximate = False
            if selected and selected["settle_mode"] == "convert":
                if _reuse_fx_rate:
                    rate = Decimal(_stored_fx_rate) if _stored_fx_rate is not None else None
                else:
                    override = await ledger.fx_override(
                        chat, selected["id"], currency, selected["settle_currency"]
                    )
                    rate, approximate = await get_rate(
                        ctx.stores.cache,
                        chat,
                        spent,
                        currency,
                        selected["settle_currency"],
                        override=override,
                    )
            payload = {
                "expense_id": str(uuid4()),
                "description": description,
                "amount": amount_minor,
                "currency": currency,
                "paid_by": payer.ref.key,
                "created_by": ctx.user.ref.key,
                "spent_on": spent.isoformat(),
                "trip_id": selected["id"] if selected else None,
                "shares": share_amounts,
                "fx_rate_to_settle": str(rate) if rate is not None else None,
            }
            if selected:
                payload["trip_settle_mode"] = selected["settle_mode"]
                payload["trip_settle_currency"] = selected["settle_currency"].upper()
            trip_label = f"({selected['name']})" if selected else "(general ledger)"
            names = _member_labels(await self._members(ctx, chat), [payer.ref.key, *share_amounts])
            if split_type == "equal":
                split_text = (
                    f"split {len(share_amounts)} ways: {_member_names(share_amounts, names)}"
                )
            else:
                split_text = "split: " + ", ".join(
                    f"{names.get(key, key)} {format_amount(value, currency)}"
                    for key, value in share_amounts.items()
                )
            payload["echo"] = (
                f"Added: {description} {format_amount(amount_minor, currency)}, "
                f"paid by {names.get(payer.ref.key, payer.display_name)}, {split_text} "
                f'{trip_label}. Say "undo" to revert.'
            )
            payload["split_type"] = split_type
            if (
                selected
                and selected["settle_mode"] == "convert"
                and currency != selected["settle_currency"].upper()
                and rate is None
            ):
                payload["echo"] += " FX: no rate found yet; set one with set_fx_rate."
            lines = [
                f"Expense: {description}",
                f"Amount: {format_amount(amount_minor, currency)}",
                f"Paid by: {payer.display_name}",
                "Shares:",
            ]
            lines.extend(
                f"- {participant_map[key].display_name}: {format_amount(value, currency)}"
                for key, value in share_amounts.items()
            )
            lines.append(
                f"Trip: {selected['name']} ({selected['status']})"
                if selected
                else "Trip: General ledger"
            )
            if selected and currency.upper() not in {
                code.upper() for code in selected["currencies"]
            }:
                codes = ", ".join(code.upper() for code in selected["currencies"])
                lines.append(
                    f"Note: {currency.upper()} is not one of this trip's currencies ({codes}). "
                    "Use update_trip to add it."
                )
            if (
                selected
                and selected["settle_mode"] == "convert"
                and currency != selected["settle_currency"].upper()
            ):
                if rate is None:
                    lines.append("FX: no rate found yet; set one with set_fx_rate")
                else:
                    suffix = " (approximate)" if approximate else ""
                    lines.append(
                        f"FX: 1 {currency} = {rate} {selected['settle_currency'].upper()}{suffix}"
                    )
            return self._pending(ctx, chat, "add_expense", payload, "\n".join(lines))
        except (ValueError, ArithmeticError) as exc:
            return ToolResult.error(str(exc))

    async def _delete_expense(self, ctx: ToolContext, *, expense_id: str) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        ledger = self._ledger(ctx.stores)
        expense = await ledger.get_expense(chat, expense_id)
        if expense is None:
            return ToolResult.error(
                "Expense reference was not found or is not unique in this chat."
            )
        if expense["created_by"] != ctx.user.ref.key and not ctx.is_admin:
            return ToolResult.error("Only the creator or an admin can delete this expense.")
        members = await self._members(ctx, chat)
        payer = next(
            (member.display_name for member in members if member.ref.key == expense["paid_by"]),
            expense["paid_by"],
        )
        member_names = {member.ref.key: member.display_name for member in members}
        shares = expense["shares"]
        share_items = (
            shares.items()
            if isinstance(shares, dict)
            else ((share["user_key"], share["amount"]) for share in shares)
        )
        share_text = "\n".join(
            f"- {member_names.get(user_key, user_key)}: "
            f"{format_amount(share_amount, expense['currency'])}"
            for user_key, share_amount in share_items
        )
        summary = (
            f"Delete expense: {expense['description']}\n"
            f"Amount: {format_amount(expense['amount'], expense['currency'])}\n"
            f"Paid by: {payer}\nSpent on: {expense['spent_on']}\nShares:\n{share_text}"
        )
        if expense.get("trip_id"):
            selected = await ledger.trip(chat, str(expense["trip_id"]))
            if selected:
                summary += f"\nTrip: {selected['name']} ({selected['status']})"
                trip_label = f"({selected['name']})"
        else:
            summary += "\nTrip: General ledger"
            trip_label = "(general ledger)"
        payload = {
            "expense_id": str(expense["id"]),
            "echo": (
                f"Deleted: {expense['description']} "
                f"{format_amount(expense['amount'], expense['currency'])} "
                f'{trip_label}. Say "undo" to bring it back.'
            ),
        }
        return self._pending(ctx, chat, "delete_expense", payload, summary)

    async def _update_expense(
        self,
        ctx: ToolContext,
        *,
        expense_id: str,
        description: str | None = None,
        amount: str | None = None,
        currency: str | None = None,
        paid_by: str | None = None,
        split_with: list[str] | None = None,
        split_type: str | None = None,
        shares: dict[str, Any] | None = None,
        spent_on: str | None = None,
        trip: str | None = None,
        general: bool | None = None,
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error("I can’t identify your group from this DM.")
        ledger = self._ledger(ctx.stores)
        expense = await ledger.get_expense(chat, expense_id)
        if expense is None:
            return ToolResult.error(
                "Expense reference was not found or is not unique in this chat."
            )
        if expense["created_by"] != ctx.user.ref.key and not ctx.is_admin:
            return ToolResult.error("Only the creator or an admin can update this expense.")
        try:
            members = await self._members(ctx, chat)
            by_key = {member.ref.key: member for member in members}
            participant_keys = [item["user_key"] for item in expense["shares"]]
            participants = [
                by_key[key].display_name if key in by_key else key for key in participant_keys
            ]
            old_trip = (
                await ledger.trip(chat, expense["trip_id"]) if expense.get("trip_id") else None
            )
            amount_text = format(
                Decimal(expense["amount"]) / (10 ** currency_exponent(expense["currency"])),
                f".{currency_exponent(expense['currency'])}f",
            )
            split_arguments_given = (
                split_with is not None or split_type is not None or shares is not None
            )
            financial_change = (
                any(value is not None for value in (amount, currency, paid_by))
                or split_arguments_given
            )
            chosen_split_with = split_with if split_with is not None else participants
            chosen_split_type = split_type or "equal"
            chosen_shares = shares
            if not split_arguments_given:
                if financial_change:
                    chosen_split_type = "shares"
                    chosen_shares = {
                        (by_key[key].display_name if key in by_key else key): str(value)
                        for key, value in (
                            (item["user_key"], item["amount"]) for item in expense["shares"]
                        )
                    }
                else:
                    chosen_split_type = "exact"
                    exponent = currency_exponent(expense["currency"])
                    chosen_shares = {
                        (
                            by_key[item["user_key"]].display_name
                            if item["user_key"] in by_key
                            else item["user_key"]
                        ): format(Decimal(item["amount"]) / (10**exponent), f".{exponent}f")
                        for item in expense["shares"]
                    }
            elif split_with is None:
                chosen_split_with = participants
                if split_type is None and shares is not None:
                    chosen_split_type = "shares"
            trip_choice = trip
            if trip_choice is None and general is not True and old_trip is not None:
                trip_choice = str(old_trip["id"])
            general_choice = general if general is not None else (old_trip is None and trip is None)
            target_trip = None
            if trip_choice is not None:
                target_trip = await _resolve_trip(ledger, chat, trip_choice)
            elif not general_choice:
                target_trip = await _current_trip(ledger, chat)
            target_currency = resolve_currency(
                currency or expense["currency"],
                target_trip["currencies"] if target_trip else None,
                trip_active=bool(target_trip),
            )
            target_date = spent_on if spent_on is not None else str(expense["spent_on"])
            reuse_fx_rate = (
                target_currency == expense["currency"]
                and target_date == str(expense["spent_on"])
                and (target_trip["id"] if target_trip else None)
                == (str(expense["trip_id"]) if expense.get("trip_id") else None)
            )
            result = await self._add_expense_impl(
                ctx,
                description=description if description is not None else expense["description"],
                amount=amount if amount is not None else amount_text,
                currency=currency or expense["currency"],
                paid_by=paid_by
                or (
                    by_key[expense["paid_by"]].display_name
                    if expense["paid_by"] in by_key
                    else expense["paid_by"]
                ),
                split_with=chosen_split_with,
                split_type=chosen_split_type,
                shares=chosen_shares,
                spent_on=spent_on or str(expense["spent_on"]),
                trip=trip_choice,
                general=general_choice,
                _reuse_fx_rate=reuse_fx_rate,
                _stored_fx_rate=(
                    str(expense["fx_rate_to_settle"])
                    if expense.get("fx_rate_to_settle") is not None
                    else None
                ),
                _resolved_payer_key=(expense["paid_by"] if paid_by is None else None),
                _resolved_participant_keys=(participant_keys if split_with is None else None),
                _resolved_share_values=(
                    {
                        item["user_key"]: (
                            str(item["amount"]) if financial_change else int(item["amount"])
                        )
                        for item in expense["shares"]
                    }
                    if not split_arguments_given
                    else None
                ),
            )
            if result.is_error or result.pending_action is None:
                return result
            fresh = result.pending_action.payload
            expected = {
                "description": expense["description"],
                "amount": expense["amount"],
                "currency": expense["currency"],
                "paid_by": expense["paid_by"],
                "spent_on": str(expense["spent_on"]),
                "trip_id": str(expense["trip_id"]) if expense.get("trip_id") else None,
                "fx_rate_to_settle": str(expense["fx_rate_to_settle"])
                if expense.get("fx_rate_to_settle") is not None
                else None,
                "shares": {item["user_key"]: item["amount"] for item in expense["shares"]},
            }
            after = {key: fresh[key] for key in expected}
            if all(
                after[field] == expected[field] for field in ("currency", "spent_on", "trip_id")
            ):
                after["fx_rate_to_settle"] = expected["fx_rate_to_settle"]
            if expected == after:
                return ToolResult.error("Nothing to change.")
            labels = _member_labels(
                members,
                [expected["paid_by"], after["paid_by"], *expected["shares"], *after["shares"]],
            )
            new_trip = await ledger.trip(chat, after["trip_id"]) if after.get("trip_id") else None
            changed = []
            if expected["description"] != after["description"]:
                changed.append(f"description {expected['description']} → {after['description']}")
            if expected["amount"] != after["amount"] or expected["currency"] != after["currency"]:
                changed.append(
                    f"amount {format_amount(expected['amount'], expected['currency'])} → "
                    f"{format_amount(after['amount'], after['currency'])}"
                )
            if expected["paid_by"] != after["paid_by"]:
                changed.append(
                    f"paid by {labels.get(expected['paid_by'], expected['paid_by'])} → "
                    f"{labels.get(after['paid_by'], after['paid_by'])}"
                )
            if expected["shares"] != after["shares"]:
                changed.append(_split_change_text(expected["shares"], after["shares"], labels))
            if expected["spent_on"] != after["spent_on"]:
                changed.append(f"date {expected['spent_on']} → {after['spent_on']}")
            if expected["trip_id"] != after["trip_id"]:
                changed.append(
                    f"trip {old_trip['name'] if old_trip else 'general ledger'} → "
                    f"{new_trip['name'] if new_trip else 'general ledger'}"
                )
            trip_label = f" ({new_trip['name']})" if new_trip else " (general ledger)"
            payload = {
                "expense_id": str(expense["id"]),
                "expected": expected,
                "after": after,
                "created_by": expense["created_by"],
                "echo": f"Updated: {after['description']}{trip_label}: "
                + "; ".join(changed)
                + '. Say "undo" to revert.',
            }
            if after["trip_id"]:
                payload["trip_settle_mode"] = fresh["trip_settle_mode"]
                payload["trip_settle_currency"] = fresh["trip_settle_currency"]
            return self._pending(ctx, chat, "update_expense", payload, payload["echo"])
        except (ValueError, ArithmeticError) as exc:
            return ToolResult.error(str(exc))

    async def _restore_expense(self, ctx: ToolContext, *, expense_id: str) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error("I can’t identify your group from this DM.")
        ledger = self._ledger(ctx.stores)
        expense = await ledger.get_expense(chat, expense_id, include_deleted=True)
        if expense is None or expense.get("deleted_at") is None:
            return ToolResult.error("Expense was not found or is not deleted.")
        if expense["created_by"] != ctx.user.ref.key and not ctx.is_admin:
            return ToolResult.error("Only the creator or an admin can restore this expense.")
        trip = await ledger.trip(chat, expense["trip_id"]) if expense.get("trip_id") else None
        if trip and trip["status"] == "closed":
            return ToolResult.error("The trip is closed. Reopen it first.")
        label = f"({trip['name']})" if trip else "(general ledger)"
        payload = {
            "expense_id": str(expense["id"]),
            "echo": (
                f"Restored: {expense['description']} "
                f"{format_amount(expense['amount'], expense['currency'])} "
                f'{label}. Say "undo" to revert.'
            ),
        }
        return self._pending(ctx, chat, "restore_expense", payload, payload["echo"])

    async def _undo_expense_change(self, ctx: ToolContext) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error("I can’t identify your group from this DM.")
        ledger = self._ledger(ctx.stores)
        latest = await ledger.latest_undoable(chat, ctx.user.ref)
        if latest is None:
            return ToolResult.error("You have no expense change to undo in this chat.")
        snapshot = latest["snapshot"]
        trip_label = f" ({latest['trip_name']})" if latest.get("trip_name") else " (general ledger)"
        summary = (
            f"Undo your last expense change: {latest['action']} of {snapshot['description']} "
            f"{format_amount(snapshot['amount'], snapshot['currency'])}{trip_label}"
        )
        return self._pending(ctx, chat, "undo_expense_change", {"event_id": latest["id"]}, summary)

    async def _update_trip_members(
        self,
        ctx: ToolContext,
        *,
        trip: str | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error("I can’t identify your group from this DM.")
        try:
            ledger = self._ledger(ctx.stores)
            selected = await self._management_trip(ledger, chat, trip)
            if not selected:
                return ToolResult.error("There are no open trips to update.")
            if not add and not remove:
                raise ValueError("Say who to add or remove.")
            add_keys = []
            for name in add or []:
                key = (await self._resolve_member(ctx, chat, name)).ref.key
                if key not in add_keys:
                    add_keys.append(key)
            remove_keys = []
            for name in remove or []:
                key = (await self._resolve_member(ctx, chat, name)).ref.key
                if key not in remove_keys:
                    remove_keys.append(key)
            payload = {
                "trip_id": selected["id"],
                "add": add_keys,
                "remove": remove_keys,
                "members_before": list(selected.get("members", [])),
            }
            old_keys = set(selected.get("members") or [])
            added = [key for key in add_keys if key not in old_keys]
            removed = [key for key in remove_keys if key in old_keys]
            final_keys = [key for key in selected.get("members", []) if key not in removed]
            final_keys.extend(key for key in added if key not in final_keys)
            labels = _member_labels(await self._members(ctx, chat), [*final_keys, *added, *removed])
            details = []
            if added:
                details.append("added " + ", ".join(labels.get(key, key) for key in added))
            if removed:
                details.append("removed " + ", ".join(labels.get(key, key) for key in removed))
            trip_label = selected["name"]
            if not trip_label.casefold().endswith(" trip"):
                trip_label += " trip"
            payload["echo"] = (
                f"{trip_label} members: "
                + (", ".join(labels.get(key, key) for key in final_keys) or "none set")
                + (f" ({'; '.join(details)})." if details else " (no changes).")
            )
            return self._pending(ctx, chat, "update_trip_members", payload, payload["echo"])
        except ValueError as exc:
            return ToolResult.error(str(exc))

    async def _settle_up(
        self,
        ctx: ToolContext,
        *,
        from_user: str,
        to_user: str,
        amount: str,
        currency: str | None = None,
        trip: str | None = None,
        general: bool = False,
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        try:
            ledger = self._ledger(ctx.stores)
            if not isinstance(general, bool):
                raise ValueError("general must be a boolean.")
            selected = (
                await _resolve_trip(ledger, chat, trip)
                if trip is not None
                else (None if general else await _current_trip(ledger, chat))
            )
            if selected and selected["status"] == "closed":
                raise ValueError("The trip is closed. Reopen it first.")
            if selected and selected["settle_mode"] == "convert":
                code = (currency or selected["settle_currency"]).upper()
                currency_exponent(code)
                if code != selected["settle_currency"].upper():
                    raise ValueError(
                        f"Settlements in this trip are in {selected['settle_currency']}."
                    )
            else:
                code = resolve_currency(
                    currency,
                    selected["currencies"] if selected else None,
                    trip_active=bool(selected),
                )
            source = await self._resolve_member(ctx, chat, from_user)
            target = await self._resolve_member(ctx, chat, to_user)
            if source.ref == target.ref:
                raise ValueError("A settlement must be between two different members.")
            minor = parse_amount(amount, code)
            payload = {
                "from_user": source.ref.key,
                "to_user": target.ref.key,
                "amount": minor,
                "currency": code,
                "trip_id": selected["id"] if selected else None,
            }
            if selected:
                payload["trip_settle_mode"] = selected["settle_mode"]
                payload["trip_settle_currency"] = selected["settle_currency"].upper()
            summary = (
                f"Settlement: {source.display_name} pays {target.display_name} "
                f"{format_amount(minor, code)}"
                + (
                    f"\nTrip: {selected['name']} ({selected['status']})"
                    if selected
                    else "\nTrip: General ledger"
                )
            )
            return self._pending(ctx, chat, "settle_up", payload, summary)
        except ValueError as exc:
            return ToolResult.error(str(exc))

    async def _set_fx_rate(
        self, ctx: ToolContext, *, currency: str, rate: str, trip: str | None = None
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        try:
            ledger = self._ledger(ctx.stores)
            selected = (
                await _resolve_trip(ledger, chat, trip)
                if trip is not None
                else await _current_trip(ledger, chat)
            )
            if (
                not selected
                or selected["status"] == "closed"
                or selected["settle_mode"] != "convert"
            ):
                raise ValueError("FX rates can only be set for a planned or active convert trip.")
            code = currency.upper()
            currency_exponent(code)
            value = Decimal(rate)
            if not value.is_finite() or value <= 0:
                raise ValueError("FX rate must be positive and finite.")
            unconverted = [
                expense
                for expense in await ledger.expenses(chat, trip_id=selected["id"])
                if expense["currency"] == code and expense["fx_rate_to_settle"] is None
            ]
            payload = {
                "trip_id": selected["id"],
                "trip_settle_mode": selected["settle_mode"],
                "trip_settle_currency": selected["settle_currency"].upper(),
                "currency": code,
                "rate": str(value),
                "expense_ids": [str(expense["id"]) for expense in unconverted],
            }
            summary = (
                f"Set FX rate: 1 {code} = {value} {selected['settle_currency']}\n"
                f"Trip: {selected['name']} ({selected['status']})"
            )
            if unconverted:
                summary += "\nApply to unconverted expenses: " + ", ".join(
                    expense["description"] for expense in unconverted
                )
            return self._pending(ctx, chat, "set_fx_rate", payload, summary)
        except (ValueError, ArithmeticError) as exc:
            return ToolResult.error(str(exc))

    async def _start_trip(
        self,
        ctx: ToolContext,
        *,
        name: str,
        countries: list[str],
        settle_mode: str,
        settle_currency: str = "SGD",
        status: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        members: list[str] | None = None,
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        try:
            ledger = self._ledger(ctx.stores)
            name = _plain_text(name)
            if not 1 <= len(name) <= 100:
                raise ValueError("Trip name must be between 1 and 100 characters.")
            countries = [_plain_text(country) for country in countries]
            if any(not country for country in countries):
                raise ValueError("Countries cannot contain empty values.")
            if any(
                t["status"] != "closed" and t["name"].casefold() == name.casefold()
                for t in await ledger.trips(chat)
            ):
                raise ValueError(f"A trip named {name} is already planned or active in this chat.")
            if settle_mode not in {"convert", "per_currency"}:
                raise ValueError("Settle mode must be convert or per_currency.")
            currencies = list(dict.fromkeys(currency_for_country(country) for country in countries))
            if not countries:
                raise ValueError("Add at least one destination country.")
            today = datetime.now(self.timezone).date()
            start = date.fromisoformat(start_date) if start_date is not None else None
            end = date.fromisoformat(end_date) if end_date is not None else None
            if start_date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date):
                raise ValueError("start_date must use YYYY-MM-DD.")
            if end_date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_date):
                raise ValueError("end_date must use YYYY-MM-DD.")
            if start and end and end < start:
                raise ValueError("End date must be on or after start date.")
            status = status or ("planned" if start and start > today else "active")
            if status not in {"planned", "active"}:
                raise ValueError("Status must be planned or active.")
            settle = settle_currency.upper()
            currency_exponent(settle)
            payload = {
                "name": name,
                "countries": countries,
                "currencies": currencies,
                "settle_mode": settle_mode,
                "settle_currency": settle,
                "status": status,
                "start_date": start.isoformat() if start else None,
                "end_date": end.isoformat() if end else None,
            }
            resolved_members = []
            for member_name in members or []:
                member = await self._resolve_member(ctx, chat, member_name)
                if member.ref.key not in resolved_members:
                    resolved_members.append(member.ref.key)
            payload["members"] = resolved_members
            lines = [
                f"{'Start' if status == 'active' else 'Plan'} trip: {name}",
                f"Countries: {', '.join(countries)}",
                f"Currencies: {', '.join(currencies)}\nSettle mode: {settle_mode}\n"
                f"Settle currency: {settle}",
            ]
            if start or end:
                lines.append(
                    f"Dates: {start.isoformat() if start else 'Not set'} → "
                    f"{end.isoformat() if end else 'Not set'}"
                )
            if resolved_members:
                member_names = _member_labels(await self._members(ctx, chat), resolved_members)
                lines.append(
                    "Members: " + ", ".join(member_names.get(key, key) for key in resolved_members)
                )
            if status == "active":
                others = [t for t in await ledger.trips(chat) if t["status"] != "closed"]
                if others:
                    lines.append(
                        "Becomes the default trip for new expenses. Other trips stay as they are: "
                        + ", ".join(f"{t['name']} ({t['status']})" for t in others)
                        + "."
                    )
            summary = "\n".join(lines)
            return self._pending(ctx, chat, "start_trip", payload, summary)
        except ValueError as exc:
            return ToolResult.error(str(exc))

    async def _update_trip(
        self,
        ctx: ToolContext,
        *,
        trip: str | None = None,
        name: Any = None,
        add_countries: Any = None,
        remove_countries: Any = None,
        add_currencies: Any = None,
        remove_currencies: Any = None,
        settle_mode: Any = None,
        settle_currency: Any = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        try:
            ledger = self._ledger(ctx.stores)
            selected = await self._management_trip(ledger, chat, trip)
            if selected is None:
                return ToolResult.error("There are no open trips to update.")
            trip = selected
            changes = {
                "name": None,
                "add_countries": None,
                "remove_countries": None,
                "add_currencies": None,
                "remove_currencies": None,
                "settle_mode": None,
                "settle_currency": None,
            }
            if name is not None:
                if not isinstance(name, str):
                    raise ValueError("name must be a string.")
                name = _plain_text(name)
                if not 1 <= len(name) <= 100:
                    raise ValueError("Trip name must be between 1 and 100 characters.")
                changes["name"] = name
            for key, value in (
                ("add_countries", add_countries),
                ("remove_countries", remove_countries),
                ("add_currencies", add_currencies),
                ("remove_currencies", remove_currencies),
            ):
                if value is None:
                    continue
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError(f"{key} must be a list of strings.")
                cleaned = [_plain_text(item) for item in value]
                if any(not item for item in cleaned):
                    raise ValueError(f"{key} cannot contain empty values.")
                if key.endswith("currencies"):
                    cleaned = [item.upper() for item in cleaned]
                    for code in cleaned:
                        currency_exponent(code)
                changes[key] = cleaned
            if settle_mode is not None:
                if not isinstance(settle_mode, str):
                    raise ValueError("settle_mode must be a string.")
                if settle_mode not in {"convert", "per_currency"}:
                    raise ValueError("Settle mode must be convert or per_currency.")
                changes["settle_mode"] = settle_mode
            if settle_currency is not None:
                if not isinstance(settle_currency, str):
                    raise ValueError("settle_currency must be a string.")
                settle_currency = settle_currency.strip().upper()
                currency_exponent(settle_currency)
                changes["settle_currency"] = settle_currency

            for key, value in (("start_date", start_date), ("end_date", end_date)):
                if value is not None:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        raise ValueError(f"{key} must use YYYY-MM-DD.")
                    changes[key] = date.fromisoformat(value).isoformat()

            new = _apply_trip_changes(trip, changes)
            for key in ("name", "settle_mode", "settle_currency", "start_date", "end_date"):
                current = trip.get(key)
                proposed = new.get(key)
                if key == "settle_currency":
                    unchanged = str(current).upper() == str(proposed).upper()
                else:
                    unchanged = current == proposed
                if unchanged:
                    changes[key] = None
            lines = _trip_change_lines(trip, new)
            if not lines:
                raise ValueError("Nothing to change on this trip.")
            if changes["remove_currencies"]:
                expenses = await ledger.expenses(chat, trip_id=trip["id"], limit=None)
                settlements = await ledger.settlements(chat, trip_id=trip["id"])
                _check_trip_change_usage(trip, new, changes, expenses, settlements)
            elif (
                trip["settle_mode"] != new["settle_mode"]
                or trip["settle_currency"].upper() != new["settle_currency"].upper()
            ):
                expenses = await ledger.expenses(chat, trip_id=trip["id"], limit=None)
                settlements = await ledger.settlements(chat, trip_id=trip["id"])
                _check_trip_change_usage(trip, new, changes, expenses, settlements)
            payload = {"trip_id": trip["id"], **changes}
            summary = "\n".join([f"Update trip: {trip['name']} ({trip['status']})", *lines])
            return self._pending(ctx, chat, "update_trip", payload, summary)
        except (ValueError, ArithmeticError) as exc:
            return ToolResult.error(str(exc))

    async def _end_trip(self, ctx: ToolContext, *, trip: str | None = None) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        ledger = self._ledger(ctx.stores)
        try:
            selected = await self._management_trip(ledger, chat, trip)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        if not selected:
            return ToolResult.error("There are no open trips to end.")
        summary = await self._balance_text(ctx, ledger, chat, selected)
        final_summary = (
            f"End trip: {selected['name']} ({selected['status']})\n"
            f"Final settle-up summary:\n{summary}"
        )
        return self._pending(
            ctx,
            chat,
            "end_trip",
            {
                "summary": final_summary,
                "trip_id": str(selected["id"]),
                "expected_status": selected["status"],
            },
            final_summary,
        )

    async def _management_trip(self, ledger: LedgerRepo, chat: ChatRef, ref: str | None):
        if ref is not None:
            selected = await _resolve_trip(ledger, chat, ref)
            if selected["status"] == "closed":
                raise ValueError("That trip is closed. Reopen it first.")
            return selected
        selected = await _current_trip(ledger, chat)
        if selected:
            return selected
        planned = await ledger.trips(chat, statuses=("planned",))
        if len(planned) == 1:
            return planned[0]
        if not planned:
            return None
        raise ValueError("Several trips are planned. Which trip do you mean?")

    async def _activate_trip(self, ctx: ToolContext, *, trip: str) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. Please use the group chat or say "
                "which group."
            )
        try:
            selected = await _resolve_trip(self._ledger(ctx.stores), chat, trip)
            if selected["status"] != "planned":
                raise ValueError("Only a planned trip can be activated.")
            summary = (
                f"Activate trip: {selected['name']} (planned) → active; becomes the default "
                "trip for new expenses."
            )
            return self._pending(
                ctx,
                chat,
                "activate_trip",
                {"trip_id": selected["id"], "expected_status": "planned"},
                summary,
            )
        except ValueError as exc:
            return ToolResult.error(str(exc))

    async def _reopen_trip(
        self, ctx: ToolContext, *, trip: str, status: str = "active"
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. Please use the group chat or say "
                "which group."
            )
        try:
            selected = await _resolve_trip(self._ledger(ctx.stores), chat, trip)
            if selected["status"] != "closed":
                raise ValueError("Only a closed trip can be reopened.")
            if status not in {"active", "planned"}:
                raise ValueError("Reopened status must be active or planned.")
            summary = (
                f"Reopen trip: {selected['name']} (closed) → {status}.\n"
                "The settle-up summary posted when it ended was informational only; no settlements "
                "were recorded by ending it, and balances are recomputed from its expenses and "
                "settlements."
            )
            return self._pending(
                ctx,
                chat,
                "reopen_trip",
                {"trip_id": selected["id"], "expected_status": "closed", "status": status},
                summary,
            )
        except ValueError as exc:
            return ToolResult.error(str(exc))

    async def _set_default_trip(self, ctx: ToolContext, *, trip: str) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. Please use the group chat or say "
                "which group."
            )
        try:
            selected = await _resolve_trip(self._ledger(ctx.stores), chat, trip)
            if selected["status"] != "active":
                raise ValueError(
                    "That trip is planned; use activate_trip before setting it as default."
                )
            return self._pending(
                ctx,
                chat,
                "set_default_trip",
                {"trip_id": selected["id"]},
                f"Set default trip: {selected['name']} (active).",
            )
        except ValueError as exc:
            return ToolResult.error(str(exc))

    async def _list_expenses(
        self, ctx: ToolContext, *, limit: int = 20, trip: str | None = None, general: bool = False
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        ledger = self._ledger(ctx.stores)
        try:
            if trip is not None and general:
                raise ValueError("Choose a trip or the general ledger, not both.")
            selected = (
                await _resolve_trip(ledger, chat, trip)
                if trip is not None
                else (None if general else await _current_trip(ledger, chat))
            )
            rows = await ledger.expenses(
                chat,
                trip_id=selected["id"] if selected else None,
                general_only=selected is None,
                limit=max(1, min(limit, 100)),
            )
        except ValueError as exc:
            return ToolResult.error(str(exc))
        if not rows:
            return ToolResult("No expenses found.")
        names = _member_labels(
            await self._members(ctx, chat),
            [key for r in rows for key in (r["paid_by"], *(s["user_key"] for s in r["shares"]))],
        )
        content = "\n".join(
            f"#{str(r['id']).replace('-', '')[:8]} {r['description']}: "
            f"{format_amount(r['amount'], r['currency'])} "
            f"(paid by {names.get(r['paid_by'], r['paid_by'])}; {_shares_text(r, names)})"
            for r in rows
        )
        return ToolResult(content)

    async def _balances(
        self, ctx: ToolContext, *, trip: str | None = None, general: bool = False
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        ledger = self._ledger(ctx.stores)
        try:
            if trip is not None and general:
                raise ValueError("Choose a trip or the general ledger, not both.")
            selected = (
                await _resolve_trip(ledger, chat, trip)
                if trip is not None
                else (None if general else await _current_trip(ledger, chat))
            )
        except ValueError as exc:
            return ToolResult.error(str(exc))
        return ToolResult(await self._balance_text(ctx, ledger, chat, selected))

    async def _member_expenses(
        self,
        ctx: ToolContext,
        *,
        member: str,
        trip: str | None = None,
        general: bool = False,
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        ledger = self._ledger(ctx.stores)
        try:
            if trip is not None and general:
                raise ValueError("Choose a trip or the general ledger, not both.")
            selected = (
                await _resolve_trip(ledger, chat, trip)
                if trip is not None
                else (None if general else await _current_trip(ledger, chat))
            )
            person = await self._resolve_member(ctx, chat, member)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        return ToolResult(await self._member_history_text(ctx, ledger, chat, selected, person))

    async def _member_history_text(
        self, ctx: ToolContext, ledger: LedgerRepo, chat: ChatRef, trip: dict | None, person: User
    ) -> str:
        scope = dict(trip_id=trip["id"] if trip else None, general_only=trip is None)
        expenses = await ledger.expenses(chat, **scope, limit=None)
        settlements = await ledger.settlements(chat, **scope)
        # Net comes from the exact code path get_balances uses, so the two always agree.
        groups, _unconverted = ledger_balances(trip, expenses, settlements)
        key = person.ref.key
        mine = [
            e
            for e in expenses
            if e["paid_by"] == key or any(s["user_key"] == key for s in e["shares"])
        ]
        my_settlements = sorted(
            (s for s in settlements if key in (s["from_user"], s["to_user"])),
            key=lambda s: s.get("created_at") or 0,
            reverse=True,
        )
        labels = _member_labels(
            await self._members(ctx, chat),
            [
                key,
                *(e["paid_by"] for e in mine),
                *(u for s in my_settlements for u in (s["from_user"], s["to_user"])),
            ],
        )
        name = labels.get(key, person.display_name)
        convert = bool(trip and trip["settle_mode"] == "convert")
        settle = trip["settle_currency"].upper() if convert else None
        scope_label = (
            f"{trip['name']}" + (f", balances in {settle}" if convert else ", per currency")
            if trip
            else "general ledger, per currency"
        )

        def to_settle(minor: int, expense: dict) -> Decimal:
            scale = Decimal(10) ** (
                currency_exponent(settle) - currency_exponent(expense["currency"])
            )
            return Decimal(minor) * Decimal(str(expense["fx_rate_to_settle"])) * scale

        def rounded(value: Decimal) -> int:
            return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))

        totals: dict[str, dict[str, Decimal]] = {}

        def add(currency: str, field: str, value: Decimal | int) -> None:
            bucket = totals.setdefault(
                currency, {f: Decimal(0) for f in ("paid", "share", "sent", "received")}
            )
            bucket[field] += Decimal(value)

        rows = []
        missing_rate = 0
        for e in mine:
            share = sum(int(s["amount"]) for s in e["shares"] if s["user_key"] == key)
            paid = int(e["amount"]) if e["paid_by"] == key else 0
            share_text = format_amount(share, e["currency"]) if share else "none"
            if convert and e["currency"].upper() != settle:
                if e.get("fx_rate_to_settle") is None:
                    missing_rate += 1
                    share_text += " (no FX rate; not in totals)"
                else:
                    share_text += f" (≈ {format_amount(rounded(to_settle(share, e)), settle)})"
                    add(settle, "paid", to_settle(paid, e))
                    add(settle, "share", to_settle(share, e))
            elif convert and e.get("fx_rate_to_settle") is None:
                missing_rate += 1
                share_text += " (no FX rate; not in totals)"
            else:
                currency = settle if convert else e["currency"]
                factor = to_settle(1, e) if convert else Decimal(1)
                add(currency, "paid", paid * factor)
                add(currency, "share", share * factor)
            spent = e.get("spent_on")
            spent_text = f"{spent.isoformat() if hasattr(spent, 'isoformat') else spent} "
            rows.append(
                f"#{str(e['id']).replace('-', '')[:8]} {spent_text if spent else ''}"
                f"{e['description']}: {format_amount(e['amount'], e['currency'])}, paid by "
                f"{labels.get(e['paid_by'], e['paid_by'])}; {name}'s share {share_text}"
            )
        settlement_rows = []
        for s in my_settlements:
            counted = not convert or s["currency"].upper() == settle
            if counted:
                add(
                    settle if convert else s["currency"],
                    "sent" if s["from_user"] == key else "received",
                    int(s["amount"]),
                )
            settlement_rows.append(
                f"- {labels.get(s['from_user'], s['from_user'])} paid "
                f"{labels.get(s['to_user'], s['to_user'])} "
                f"{format_amount(s['amount'], s['currency'])}"
                + ("" if counted else f" (not counted: balances are in {settle})")
            )

        cap = 100
        lines = [
            f"{name} — {scope_label}: {len(mine)} expense(s) they paid or share, "
            f"{len(my_settlements)} settlement(s). Expenses they're not in are left out."
        ]
        lines.extend(rows[:cap])
        if len(rows) > cap:
            lines.append(f"{len(rows) - cap} older expense(s) not shown; totals include them.")
        if settlement_rows:
            lines.append("Settlements:")
            lines.extend(settlement_rows[:cap])
            if len(settlement_rows) > cap:
                lines.append(
                    f"{len(settlement_rows) - cap} older settlement(s) not shown; "
                    "totals include them."
                )
        if missing_rate:
            lines.append(
                f"{missing_rate} expense(s) have no FX rate and aren't in the totals; "
                "use set_fx_rate."
            )
        currencies = sorted(set(totals) | {c for c, v in groups.items() if v.get(key)})
        if convert:
            currencies = [settle]
        if not currencies:
            lines.append(f"Totals: nothing recorded; net: {name} is settled.")
        for currency in currencies:
            bucket = totals.get(currency) or {
                f: Decimal(0) for f in ("paid", "share", "sent", "received")
            }
            net = groups.get(currency, {}).get(key, 0)
            parts = {field: rounded(value) for field, value in bucket.items()}
            if net > 0:
                net_text = f"{name} is owed {format_amount(net, currency)}"
            elif net < 0:
                net_text = f"{name} owes {format_amount(-net, currency)}"
            else:
                net_text = f"{name} is settled"
            line = (
                f"Totals in {currency}: paid {format_amount(parts['paid'], currency)}; "
                f"own share {format_amount(parts['share'], currency)}; settlements sent "
                f"{format_amount(parts['sent'], currency)}, received "
                f"{format_amount(parts['received'], currency)}; net: {net_text} "
                "(same as get_balances)."
            )
            drift = net - (parts["paid"] - parts["share"] + parts["sent"] - parts["received"])
            if drift:
                line += (
                    f" Net includes a {format_amount(abs(drift), currency)} rounding adjustment."
                )
            lines.append(line)
        return "\n".join(lines)

    async def _trip(self, ctx: ToolContext, *, trip: str | None = None) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        ledger = self._ledger(ctx.stores)
        try:
            selected = (
                await _resolve_trip(ledger, chat, trip)
                if trip is not None
                else await self._management_trip(ledger, chat, None)
            )
        except ValueError as exc:
            return ToolResult.error(str(exc))
        if not selected:
            opened = await ledger.trips(chat, statuses=("planned", "active"))
            if not opened:
                return ToolResult("There are no planned or active trips.")
            return ToolResult(
                "Open trips: " + "; ".join(f"{item['name']} ({item['status']})" for item in opened)
            )
        return ToolResult(
            f"{selected['name']} ({selected['status']}"
            f"{', default' if selected.get('is_default') else ''}): "
            f"{', '.join(selected['countries'])}; {', '.join(selected['currencies'])}; "
            f"{selected['settle_mode']} in {selected['settle_currency']}; "
            f"dates {selected.get('start_date') or 'not set'} → "
            f"{selected.get('end_date') or 'not set'}.\n"
            + await self._trip_members_text(ctx, chat, selected)
        )

    async def _list_trips(self, ctx: ToolContext) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. Please use the group chat or say "
                "which group."
            )
        trips = await self._ledger(ctx.stores).trips(chat, closed_limit=5)
        if not trips:
            return ToolResult("There are no trips.")
        lines = []
        for item in trips:
            dates = ""
            if item.get("start_date") or item.get("end_date"):
                dates = (
                    f"; {item.get('start_date') or 'not set'} → {item.get('end_date') or 'not set'}"
                )
            marker = " (default)" if item.get("is_default") else ""
            lines.append(
                f"{item['name']} — {item['status']}{marker}{dates}; "
                f"{', '.join(item['countries'])}; {', '.join(item['currencies'])}; "
                f"{item['settle_mode']} in {item['settle_currency']}; "
                f"{await self._trip_members_text(ctx, chat, item)}"
            )
        return ToolResult("\n".join(lines))

    async def _trip_members_text(self, ctx: ToolContext, chat: ChatRef, trip: dict) -> str:
        keys = trip.get("members") or []
        if not keys:
            return "Members: none set"
        names = _member_labels(await self._members(ctx, chat), keys)
        return "Members: " + ", ".join(names.get(key, key) for key in keys)

    async def _balance_text(
        self, ctx: ToolContext, ledger: LedgerRepo, chat: ChatRef, trip: dict | None
    ) -> str:
        display_names = {
            member.ref.key: member.display_name for member in await self._members(ctx, chat)
        }

        def label(user_key: str) -> str:
            return display_names.get(user_key, user_key)

        expenses = await ledger.expenses(
            chat,
            trip_id=trip["id"] if trip else None,
            general_only=trip is None,
            limit=None,
        )
        settlements = await ledger.settlements(
            chat, trip_id=trip["id"] if trip else None, general_only=trip is None
        )
        groups, unconverted = ledger_balances(trip, expenses, settlements)
        lines = []
        for currency, values in sorted(groups.items()):
            values = {u: v for u, v in values.items() if v}
            if not values:
                continue
            lines.append(f"{currency} balances:")
            lines.extend(
                f"- {label(user)} {'is owed' if value > 0 else 'owes'} "
                f"{format_amount(abs(value), currency)}"
                for user, value in sorted(values.items())
            )
            lines.extend(
                f"- {label(source)} pays {label(target)} {format_amount(amount, currency)}"
                for source, target, amount in simplify_balances(values)
            )
        if unconverted:
            lines.append(f"{len(unconverted)} expense(s) have no FX rate; use set_fx_rate.")
        if not lines:
            return "All balances are settled."
        return "\n".join(lines)

    async def _command_balance(self, ctx: CommandContext, _text: str) -> str:
        tool_ctx = ToolContext(
            ctx.chat,
            ctx.user,
            ctx.is_dm,
            ctx.platform,
            ctx.stores,
            thread_id=ctx.thread_id,
            is_admin=ctx.is_admin,
        )
        result = await self._balances(tool_ctx)
        return result.content

    async def _command_expenses(self, ctx: CommandContext, _text: str) -> str:
        tool_ctx = ToolContext(
            ctx.chat,
            ctx.user,
            ctx.is_dm,
            ctx.platform,
            ctx.stores,
            thread_id=ctx.thread_id,
            is_admin=ctx.is_admin,
        )
        return (await self._list_expenses(tool_ctx, limit=10)).content

    async def _command_export(self, ctx: CommandContext, text: str) -> str:
        tool_ctx = ToolContext(
            ctx.chat,
            ctx.user,
            ctx.is_dm,
            ctx.platform,
            ctx.stores,
            thread_id=ctx.thread_id,
            is_admin=ctx.is_admin,
        )
        scope = _plain_text(text)
        if not scope:
            result = await self._export(tool_ctx)
        elif scope.casefold() == "general":
            result = await self._export(tool_ctx, general=True)
        else:
            result = await self._export(tool_ctx, trip=scope)
        return result.content

    async def sheet_export_data(
        self, ctx: ToolContext, trip: str | None = None, general: bool = False
    ) -> dict:
        """Public, read-only ledger snapshot for app-injected document exporters.

        Uses the same group/DM and trip resolution as CSV export. Original currency totals
        include expense records only, never shares/settlements and never an implicit FX rate.
        """
        chat = await self._chat(ctx)
        if chat is None:
            raise ValueError("I can't identify your group from this DM. Use the group chat.")
        if (
            type(general) is not bool
            or trip is not None
            and (not isinstance(trip, str) or not trip)
        ):
            raise ValueError("Choose a valid trip or the general ledger.")
        if trip is not None and general:
            raise ValueError("Choose a trip or the general ledger, not both.")
        ledger = self._ledger(ctx.stores)
        selected = (
            None
            if general
            else (
                await _resolve_trip(ledger, chat, trip)
                if trip
                else await _current_trip(ledger, chat)
            )
        )
        expenses = await ledger.expenses(
            chat,
            trip_id=selected["id"] if selected else None,
            general_only=selected is None,
            limit=None,
        )
        settlements = await ledger.settlements(
            chat, trip_id=selected["id"] if selected else None, general_only=selected is None
        )
        names = {member.ref.key: member.display_name for member in await self._members(ctx, chat)}
        exported = build_export(
            selected,
            expenses,
            settlements,
            names,
            today=datetime.now(self.timezone).date(),
            timezone=self.timezone,
        )
        totals = {}
        for expense in expenses:
            currency = expense["currency"]
            amount = Decimal(expense["amount"]) / (10 ** currency_exponent(currency))
            totals[currency] = totals.get(currency, Decimal(0)) + amount
        return {
            "chat_key": chat.key,
            "trip_id": str(selected["id"]) if selected else None,
            "trip_name": str(selected["name"]) if selected else "General ledger",
            "totals": {code: str(value) for code, value in totals.items()},
            "csv": exported.data,
        }

    async def _export(
        self, ctx: ToolContext, *, trip: str | None = None, general: bool = False
    ) -> ToolResult:
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. "
                "Please use the group chat or say which group."
            )
        if not isinstance(ctx.platform, FilePlatform):
            return ToolResult.error("This chat platform can't send files.")
        if trip is not None and (not isinstance(trip, str) or not trip):
            return ToolResult.error("Trip must be a non-empty string.")
        if not isinstance(general, bool):
            return ToolResult.error("General must be a boolean.")
        if trip is not None and general:
            return ToolResult.error("Choose a trip or the general ledger, not both.")

        ledger = self._ledger(ctx.stores)
        if general:
            selected = None
        elif trip is not None:
            try:
                selected = await _resolve_trip(ledger, chat, trip)
            except ValueError as exc:
                return ToolResult.error(str(exc))
        else:
            try:
                selected = await _current_trip(ledger, chat)
            except ValueError as exc:
                return ToolResult.error(str(exc))
        expenses = await ledger.expenses(
            chat,
            trip_id=selected["id"] if selected else None,
            general_only=selected is None,
            limit=None,
        )
        settlements = await ledger.settlements(
            chat, trip_id=selected["id"] if selected else None, general_only=selected is None
        )
        names = {member.ref.key: member.display_name for member in await self._members(ctx, chat)}
        exported = build_export(
            selected,
            expenses,
            settlements,
            names,
            today=datetime.now(self.timezone).date(),
            timezone=self.timezone,
        )
        caption = export_caption(selected, exported.expense_count, exported.settlement_count)
        try:
            await ctx.platform.send_file(
                ctx.chat,
                exported.filename,
                exported.data,
                caption=caption,
                thread_id=ctx.thread_id,
            )
        except PlatformError as exc:
            return ToolResult.error(f"Couldn't send the file ({exc}).")
        logger.info(
            "expense export chat=%s expenses=%d settlements=%d bytes=%d",
            chat.key,
            exported.expense_count,
            exported.settlement_count,
            len(exported.data),
        )
        return ToolResult(
            f"Sent {exported.filename}: {exported.expense_count} expenses, "
            f"{exported.settlement_count} settlements."
        )

    async def _read_receipt(self, ctx: ToolContext, *, image: int = 1) -> ToolResult:
        if not ctx.capabilities.files:
            return ToolResult.error("This chat platform can't share images.")
        attachments = tuple(att for att in ctx.turn.attachments if is_image(att))
        if not attachments:
            return ToolResult.error("Please send the receipt as a photo or reply to its message.")
        if isinstance(image, bool) or not isinstance(image, int):
            return ToolResult.error("The image number must be a 1-based integer.")
        if image < 1 or image > len(attachments):
            return ToolResult.error(f"There are {len(attachments)} image attachment(s).")
        if self.limits and await self.limits.budget_exhausted():
            return ToolResult.error("The bot has reached today's token budget.")
        attachment = attachments[image - 1]
        image_visible = ctx.turn.vision and attachment.id in ctx.turn.downloads
        reader_only_error = (
            "The receipt reader couldn't process this image, but you can see it. Read the total, "
            "currency and date from the image yourself and continue (call add_expense). "
            "Don't mention this to the user."
        )
        try:
            image_part = await self.receipt_reader.load(
                ctx.platform,
                attachment,
                chat=ctx.chat,
                cache=ctx.turn.downloads,
            )
        except ImageUnavailable as exc:
            if exc.reason == "download_failed":
                return ToolResult.error(
                    reader_only_error
                    if image_visible
                    else "I couldn't download that photo from Telegram. Please send it again."
                )
            return ToolResult.error(
                {
                    "download_failed": "I couldn't download that photo from Telegram. "
                    "Please send it again.",
                    "too_large": "That image is over 5 MB. Please send a smaller one.",
                    "unsupported_format": "I can only read JPEG, PNG or WebP images.",
                }[exc.reason]
            )
        day = datetime.now(self.timezone).date().isoformat()
        count = await ctx.stores.cache.incr(f"receipt:user:{ctx.user.ref.key}:{day}", ttl_s=86400)
        if count > self.receipt_settings.RECEIPT_READS_PER_USER_PER_DAY:
            return ToolResult.error("You've reached today's receipt-reading limit.")
        started = time.monotonic()
        try:
            receipt, usage = await self.receipt_reader.read(
                image_part,
                session_key=f"{ctx.chat.key}:receipt",
                today=datetime.now(self.timezone).date(),
                chat_key=ctx.chat.key,
            )
        except ReceiptError as exc:
            usage = exc.usage
            if self.limits:
                await self.limits.record(usage.input_tokens + usage.output_tokens)
            logger.info(
                "receipt read chat=%s input_tokens=%d output_tokens=%d "
                "items=0 warnings=0 latency_ms=%d",
                ctx.chat.key,
                usage.input_tokens,
                usage.output_tokens,
                int((time.monotonic() - started) * 1000),
            )
            return ToolResult.error(reader_only_error if image_visible else str(exc))
        except Exception as exc:
            logger.warning("receipt read failed chat=%s error=%s", ctx.chat.key, type(exc).__name__)
            return ToolResult.error(
                reader_only_error
                if image_visible
                else "I couldn't read that receipt right now. Please try again later."
            )
        if self.limits:
            await self.limits.record(usage.input_tokens + usage.output_tokens)
        logger.info(
            "receipt read chat=%s input_tokens=%d output_tokens=%d "
            "items=%d warnings=%d latency_ms=%d",
            ctx.chat.key,
            usage.input_tokens,
            usage.output_tokens,
            len(receipt.items),
            len(receipt.warnings),
            int((time.monotonic() - started) * 1000),
        )
        return ToolResult(
            content=(
                "Receipt fields extracted by a vision model (unverified; "
                "confirm with the user before saving):\n" + receipt.to_json()
            ),
            untrusted=True,
        )

    async def _send_expense_card(
        self, ctx: ToolContext, *, trip=None, general=False, summary=False
    ) -> ToolResult:
        if trip is not None and (not isinstance(trip, str) or not trip):
            return ToolResult.error("Trip must be a non-empty string.")
        if not isinstance(general, bool):
            return ToolResult.error("General must be a boolean.")
        if trip is not None and general:
            return ToolResult.error("Choose a trip or the general ledger, not both.")
        if not self.cards or ctx.turn.tool_attempts.get("expense_card"):
            return ToolResult.error(
                "An expense card is unavailable or was already attempted this turn."
            )
        if not can_deliver_cards(ctx.platform):
            return ToolResult.error("Cards are unavailable here.")
        ctx.turn.tool_attempts["expense_card"] = 1
        chat = await self._chat(ctx)
        if chat is None:
            return ToolResult.error(
                "I can’t identify your group from this DM. Please use the group chat or "
                "say which group."
            )
        ledger = self._ledger(ctx.stores)
        try:
            selected = (
                await _resolve_trip(ledger, chat, trip)
                if trip is not None
                else (None if general else await _current_trip(ledger, chat))
            )
        except ValueError as exc:
            return ToolResult.error(str(exc))
        scope = dict(trip_id=selected["id"] if selected else None, general_only=selected is None)
        expenses = await ledger.expenses(chat, **scope, limit=None)
        settlements = await ledger.settlements(chat, **scope)
        groups, unconverted = ledger_balances(selected, expenses, settlements)
        names = {member.ref.key: member.display_name for member in await self._members(ctx, chat)}
        totals = {}
        for expense in expenses:
            code = expense["currency"]
            amount = Decimal(int(expense["amount"])) / (10 ** currency_exponent(code))
            totals[code] = totals.get(code, Decimal(0)) + amount
        totals = {code: str(value) for code, value in totals.items()}
        try:
            builder = trip_summary_card if summary else balance_card
            card = builder(
                selected,
                totals,
                groups,
                names,
                expenses=expenses,
                unconverted=unconverted,
            )
        except (CardError, ValueError):
            return ToolResult.error(
                "Too many entries for a card; here are the balances as text.\n"
                + await self._balance_text(ctx, ledger, chat, selected)
            )
        try:
            pages = await self.cards.render(card, theme=self.cards_theme)
            try:
                delivery_chat = await self._chat(ctx)
            except Exception:
                delivery_chat = None
            if not can_deliver_cards(ctx.platform) or delivery_chat != chat:
                return ToolResult.error("Card delivery is no longer allowed.")
            await send_card_pages(
                ctx.platform, ctx.chat, pages, caption="📋 " + card.title, thread_id=ctx.thread_id
            )
            return ToolResult(
                "Card sent above the text reply. Now reply in 1–2 short lines with the "
                "headline result."
            )
        except CardError as exc:
            if exc.code in {"invalid", "too_large"}:
                return ToolResult.error(
                    "Too many entries for a card; here are the balances as text.\n"
                    + await self._balance_text(ctx, ledger, chat, selected)
                )
            return ToolResult.error("Couldn't make the card; answer in text.")
        except ValueError:
            return ToolResult.error("Couldn't make the card; answer in text.")

    async def _send_balance_card(self, ctx, *, trip=None, general=False):
        return await self._send_expense_card(ctx, trip=trip, general=general)

    async def _send_trip_summary_card(self, ctx, *, trip=None):
        return await self._send_expense_card(ctx, trip=trip, summary=True)

    def tools(self) -> list[Tool]:
        obj = {"type": "object"}
        tools = (
            [
                Tool(
                    "read_receipt",
                    "Read a receipt photo or screenshot attached to this message or its reply. "
                    "Returns unverified fields and never saves anything. Then propose add_expense "
                    "with the total; pass trip to add_expense when the user names a trip. "
                    "Ask who paid and how to split if unclear.",
                    {
                        **obj,
                        "properties": {
                            "image": {
                                "description": "Receipt attachment index; defaults to 1.",
                                "type": "integer",
                                "minimum": 1,
                                "default": 1,
                            }
                        },
                        "required": [],
                    },
                    self._read_receipt,
                )
            ]
            if self.receipt_reader
            else []
        ) + [
            Tool(
                "add_expense",
                "Record an expense in a trip or general ledger. Applies immediately "
                "(may ask the user to confirm after web or file content); "
                "update_expense, delete_expense or "
                "undo_expense_change can undo it.",
                {
                    **obj,
                    "properties": {
                        "description": {"type": "string", "description": "What was purchased."},
                        "amount": {
                            "type": "string",
                            "description": "Decimal string without a symbol, such as 12.50.",
                        },
                        "currency": {
                            "type": "string",
                            "description": "ISO 4217 code; convert ¥ or yen to JPY. Omit "
                            "to use trip/default currency.",
                        },
                        "paid_by": {
                            "type": "string",
                            "description": MEMBER_NAME_DESCRIPTION,
                        },
                        "split_with": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "description": MEMBER_NAME_DESCRIPTION,
                            },
                            "description": "People sharing the cost; "
                            "when omitted, trip members are used; explicit values override them.",
                        },
                        "split_type": {
                            "type": "string",
                            "enum": ["equal", "exact", "percentage", "shares"],
                            "description": "Split method: equal (default), exact, percentage, "
                            "or shares.",
                        },
                        "shares": {
                            "type": "object",
                            "description": MEMBER_NAME_DESCRIPTION
                            + " "
                            + (
                                'Use split_with names as keys: exact {name: "12.50"}; '
                                "percentage {name: 25}; shares {name: 2}."
                            ),
                        },
                        "spent_on": {
                            "type": "string",
                            "description": "Expense date as YYYY-MM-DD; defaults to today.",
                        },
                        "trip": {
                            "type": "string",
                            "description": "Trip name or ID; planned trips are allowed.",
                        },
                        "general": {
                            "type": "boolean",
                            "description": "Use the general ledger explicitly.",
                        },
                    },
                    "required": ["description", "amount", "paid_by"],
                },
                self._add_expense,
                writes=True,
                direct=True,
                # Receipt output remains untrusted; this exemption applies only to constrained adds.
                direct_after=frozenset({"read_receipt"}),
            ),
            Tool(
                "update_expense",
                "Update an expense you created (or any as admin). Applies immediately "
                "(may ask the user to confirm after web or file content); "
                "undo_expense_change undoes it.",
                {
                    **obj,
                    "properties": {
                        "expense_id": {
                            "description": "ID from list_expenses: UUID or 8-character reference.",
                            "type": "string",
                        },
                        "description": {
                            "description": "Updated item description; omitted keeps the "
                            "current description.",
                            "type": "string",
                        },
                        "amount": {
                            "description": "Decimal amount string without currency symbol; "
                            "omitted keeps the current amount.",
                            "type": "string",
                        },
                        "currency": {
                            "description": "ISO 4217 currency code; omitted keeps the "
                            "current currency.",
                            "type": "string",
                        },
                        "paid_by": {
                            "description": MEMBER_NAME_DESCRIPTION + " Omitted keeps the "
                            "current payer.",
                            "type": "string",
                        },
                        "split_with": {
                            "description": MEMBER_NAME_DESCRIPTION + " Omitted keeps existing "
                            "participants.",
                            "type": "array",
                            "items": {"type": "string", "description": MEMBER_NAME_DESCRIPTION},
                        },
                        "split_type": {
                            "description": "Shares without split_type use shares only when "
                            "split_with is omitted. With split_with, omitted split_type "
                            "means equal; "
                            "pass split_type='shares' explicitly. Omit all split fields to "
                            "preserve existing shares.",
                            "type": "string",
                            "enum": ["equal", "exact", "percentage", "shares"],
                        },
                        "shares": {
                            "description": "Per-person amounts, percentages, or share weights.",
                            "type": "object",
                        },
                        "spent_on": {
                            "description": "Expense date (YYYY-MM-DD); omission keeps the "
                            "current date.",
                            "type": "string",
                        },
                        "trip": {
                            "description": "Trip name or ID from list_trips; omitted keeps the "
                            "current trip or ledger.",
                            "type": "string",
                        },
                        "general": {
                            "description": "Use the general ledger outside trips.",
                            "type": "boolean",
                        },
                    },
                    "required": ["expense_id"],
                },
                self._update_expense,
                writes=True,
                direct=True,
            ),
            Tool(
                "restore_expense",
                "Restore an expense you created. Applies immediately (may ask the user to confirm "
                "after web or file content); undo_expense_change undoes it.",
                {
                    **obj,
                    "properties": {
                        "expense_id": {
                            "description": "ID from list_expenses: UUID or 8-character reference.",
                            "type": "string",
                        }
                    },
                    "required": ["expense_id"],
                },
                self._restore_expense,
                writes=True,
                direct=True,
            ),
            Tool(
                "undo_expense_change",
                "Undo your latest expense change in this chat. Applies immediately "
                "(may ask the user to confirm after web or file content).",
                {**obj, "properties": {}},
                self._undo_expense_change,
                writes=True,
                direct=True,
            ),
            Tool(
                "update_trip_members",
                "Change an open trip's default split members. Applies immediately "
                "(may ask the user to confirm after web or file content); "
                "reverse add/remove to undo.",
                {
                    **obj,
                    "properties": {
                        "trip": {
                            "description": "Trip name or ID from list_trips; omitted uses the "
                            "current trip or sole planned trip.",
                            "type": "string",
                        },
                        "add": {
                            "description": MEMBER_NAME_DESCRIPTION,
                            "type": "array",
                            "items": {"type": "string", "description": MEMBER_NAME_DESCRIPTION},
                        },
                        "remove": {
                            "description": MEMBER_NAME_DESCRIPTION,
                            "type": "array",
                            "items": {"type": "string", "description": MEMBER_NAME_DESCRIPTION},
                        },
                    },
                },
                self._update_trip_members,
                writes=True,
                direct=True,
            ),
            Tool(
                "delete_expense",
                "Delete an expense you created (or any as admin). Applies immediately "
                "(may ask the user to confirm after web or file content); "
                "restore_expense undoes it.",
                {
                    **obj,
                    "properties": {
                        "expense_id": {
                            "type": "string",
                            "description": "ID from list_expenses: UUID or 8-character reference.",
                        }
                    },
                    "required": ["expense_id"],
                },
                self._delete_expense,
                writes=True,
                direct=True,
            ),
            Tool(
                "settle_up",
                "Propose a settlement for the current/default or named trip; general=true selects "
                "the general ledger. Requires confirmation.",
                {
                    **obj,
                    "properties": {
                        "from_user": {
                            "type": "string",
                            "description": MEMBER_NAME_DESCRIPTION,
                        },
                        "to_user": {
                            "type": "string",
                            "description": MEMBER_NAME_DESCRIPTION,
                        },
                        "amount": {
                            "type": "string",
                            "description": "Decimal string without a symbol, such as 12.50.",
                        },
                        "currency": {
                            "type": "string",
                            "description": "ISO code; omit to use trip/default currency.",
                        },
                        "trip": {
                            "type": "string",
                            "description": "Trip name or ID to choose the trip for this "
                            "settlement.",
                        },
                        "general": {
                            "description": "Use the general ledger outside trips.",
                            "type": "boolean",
                        },
                    },
                    "required": ["from_user", "to_user", "amount"],
                },
                self._settle_up,
                writes=True,
            ),
            Tool(
                "set_fx_rate",
                "Optional: override the automatic FX rate for a currency in a named convert trip, "
                "e.g. to match a money changer. Pass trip by name when several trips exist. "
                "Applies to later expenses in that currency "
                "and fills expenses that have no rate. Requires confirmation.",
                {
                    **obj,
                    "properties": {
                        "currency": {"type": "string", "description": "Source ISO currency code."},
                        "rate": {
                            "type": "string",
                            "description": "Decimal settle-currency units per one source unit.",
                        },
                        "trip": {"type": "string", "description": "Trip name or ID."},
                    },
                    "required": ["currency", "rate"],
                },
                self._set_fx_rate,
                writes=True,
            ),
            Tool(
                "start_trip",
                "Start a trip without ending another. Set future trips to planned; other tools "
                "select trips by name or ID. Requires confirmation.",
                {
                    **obj,
                    "properties": {
                        "name": {"type": "string", "description": "Short trip name."},
                        "countries": {
                            "description": "Destination countries by name or ISO code.",
                            "type": "array",
                            "items": {
                                "type": "string",
                                "description": "Destination country name or ISO code.",
                            },
                        },
                        "settle_mode": {
                            "type": "string",
                            "enum": ["convert", "per_currency"],
                            "description": "convert combines balances in settle_currency; "
                            "per_currency keeps separate currency balances.",
                        },
                        "settle_currency": {
                            "type": "string",
                            "description": "ISO code used by convert mode; defaults to SGD.",
                        },
                        "status": {
                            "description": "Trip status: planned or active; defaults to planned "
                            "for a future start_date, otherwise active.",
                            "type": "string",
                            "enum": ["planned", "active"],
                        },
                        "start_date": {
                            "type": "string",
                            "description": "Start date (YYYY-MM-DD); determines planned vs active "
                            "status if status is omitted.",
                        },
                        "end_date": {"type": "string", "description": "YYYY-MM-DD, informational."},
                        "members": {
                            "type": "array",
                            "items": {"type": "string", "description": MEMBER_NAME_DESCRIPTION},
                            "description": MEMBER_NAME_DESCRIPTION,
                        },
                    },
                    "required": ["name", "countries", "settle_mode"],
                },
                self._start_trip,
                writes=True,
            ),
            Tool(
                "update_trip",
                "Change a named planned/active trip: rename; add/remove countries (adding one adds "
                "its currency) or currencies; change settle mode/currency before expenses. "
                "Applies immediately (may ask the user to confirm after web or file content); "
                "restore prior values to undo.",
                {
                    **obj,
                    "properties": {
                        "name": {"description": "New trip name when renaming.", "type": "string"},
                        "add_countries": {
                            "description": "Countries to add by name or ISO code.",
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "remove_countries": {
                            "description": "Countries to remove by name or ISO code.",
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "add_currencies": {
                            "description": "ISO 4217 codes to add.",
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "remove_currencies": {
                            "description": "ISO 4217 codes to remove.",
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "settle_mode": {
                            "description": "Mode: convert or per_currency.",
                            "type": "string",
                            "enum": ["convert", "per_currency"],
                        },
                        "settle_currency": {
                            "description": "ISO 4217 code; omitted keeps the current value.",
                            "type": "string",
                        },
                        "trip": {"type": "string", "description": "Trip name or ID."},
                        "start_date": {"type": "string", "description": "YYYY-MM-DD."},
                        "end_date": {"type": "string", "description": "YYYY-MM-DD."},
                    },
                    "required": [],
                },
                self._update_trip,
                writes=True,
                direct=True,
            ),
            Tool(
                "end_trip",
                "Close a named/current trip with balance summary. Only end the trip shown in "
                "the confirmation. Requires confirmation.",
                {
                    **obj,
                    "properties": {
                        "trip": {
                            "description": "Trip name or ID from list_trips; omitted uses the "
                            "current trip or sole planned trip.",
                            "type": "string",
                        }
                    },
                },
                self._end_trip,
                writes=True,
            ),
            Tool(
                "list_expenses",
                "List recent expense items. Not for totals/who owes → get_balances; "
                "full ledger CSV → export_expenses.",
                {
                    **obj,
                    "properties": {
                        "limit": {
                            "description": "Recent item count; default 20, range 1–100.",
                            "type": "integer",
                        },
                        "trip": {
                            "description": "Trip name or ID from list_trips; omitted uses the "
                            "default or sole active trip; if none is active, uses the general "
                            "ledger. Specify a trip if several are active.",
                            "type": "string",
                        },
                        "general": {
                            "description": "Use the general ledger outside trips.",
                            "type": "boolean",
                        },
                    },
                },
                self._list_expenses,
            ),
            Tool(
                "get_balances",
                "Show who owes whom. Not for expense item lists → list_expenses.",
                {
                    **obj,
                    "properties": {
                        "trip": {
                            "description": "Trip name or ID from list_trips; omitted uses the "
                            "default or sole active trip; if none is active, uses the general "
                            "ledger. Specify a trip if several are active.",
                            "type": "string",
                        },
                        "general": {
                            "description": "Use the general ledger outside trips.",
                            "type": "boolean",
                        },
                    },
                },
                self._balances,
            ),
            Tool(
                "member_expenses",
                "One member's expense history: every expense they paid or share, with their "
                "share, plus settlements and a net that matches get_balances. Use to explain "
                "one person's balance.",
                {
                    **obj,
                    "required": ["member"],
                    "properties": {
                        "member": {"description": MEMBER_NAME_DESCRIPTION, "type": "string"},
                        "trip": {
                            "description": "Trip name or ID from list_trips; omitted uses the "
                            "default or sole active trip; if none is active, uses the general "
                            "ledger. Specify a trip if several are active.",
                            "type": "string",
                        },
                        "general": {
                            "description": "Use the general ledger outside trips.",
                            "type": "boolean",
                        },
                    },
                },
                self._member_expenses,
            ),
            Tool(
                "export_expenses",
                "Send the full expense ledger as a CSV file for spreadsheet review.",
                {
                    **obj,
                    "properties": {
                        "trip": {
                            "description": "Trip name or ID from list_trips; omitted uses the "
                            "default or sole active trip; if none is active, uses the general "
                            "ledger. Specify a trip if several are active.",
                            "type": "string",
                        },
                        "general": {
                            "description": "Use the general ledger outside trips.",
                            "type": "boolean",
                        },
                    },
                },
                self._export,
            ),
            Tool(
                "list_trips",
                "List planned and active trips plus the five most recently closed.",
                {**obj, "properties": {}},
                self._list_trips,
            ),
            Tool(
                "get_trip",
                "Show a named trip or current trip; use list_trips when unsure.",
                {
                    **obj,
                    "properties": {
                        "trip": {
                            "description": "Trip name or ID from list_trips; omitted uses the "
                            "default or sole active trip, else the sole planned trip. Specify a "
                            "trip if the choice is ambiguous.",
                            "type": "string",
                        }
                    },
                },
                self._trip,
            ),
            Tool(
                "activate_trip",
                "Activate a planned trip and make it the default. Requires confirmation.",
                {
                    **obj,
                    "properties": {
                        "trip": {
                            "description": "Trip name or ID from list_trips; required.",
                            "type": "string",
                        }
                    },
                    "required": ["trip"],
                },
                self._activate_trip,
                writes=True,
            ),
            Tool(
                "reopen_trip",
                "Reopen a closed trip as active or planned; its ending summary was informational "
                "only. Requires confirmation.",
                {
                    **obj,
                    "properties": {
                        "trip": {
                            "description": "Trip name or ID from list_trips; required.",
                            "type": "string",
                        },
                        "status": {
                            "description": "Trip status after reopening: planned or active; "
                            "defaults to active.",
                            "type": "string",
                            "enum": ["active", "planned"],
                        },
                    },
                    "required": ["trip"],
                },
                self._reopen_trip,
                writes=True,
            ),
            Tool(
                "set_default_trip",
                "Choose the active trip used for new expenses when no trip is named. Requires "
                "confirmation.",
                {
                    **obj,
                    "properties": {
                        "trip": {
                            "description": "Trip name or ID from list_trips; required.",
                            "type": "string",
                        }
                    },
                    "required": ["trip"],
                },
                self._set_default_trip,
                writes=True,
            ),
        ]
        if self.cards:
            optional = {
                **obj,
                "properties": {
                    "trip": {"type": "string", "description": "Trip name or ID."},
                    "general": {"type": "boolean", "description": "Use the general ledger."},
                },
            }
            tools.extend(
                [
                    Tool(
                        "send_balance_card",
                        "Send balances and who-pays-whom as one card image; figures come from the "
                        "ledger. Use when someone asks for a card or picture of balances.",
                        optional,
                        self._send_balance_card,
                    ),
                    Tool(
                        "send_trip_summary_card",
                        "Send a trip summary card (balances plus spend by category or day); "
                        "figures come from the ledger.",
                        {**obj, "properties": {"trip": optional["properties"]["trip"]}},
                        self._send_trip_summary_card,
                    ),
                ]
            )
        return tools

    def commands(self) -> list[Command]:
        return [
            Command("balance", "Show ledger balances.", self._command_balance),
            Command("expenses", "List the last 10 expenses.", self._command_expenses),
            Command(
                "export",
                "Send the ledger as a CSV file (/export [trip name | general]).",
                self._command_export,
            ),
        ]

    def action_handlers(self) -> dict[str, PendingActionHandler]:
        return {
            kind: _CommitHandler(self)
            for kind in (
                "add_expense",
                "delete_expense",
                "update_expense",
                "restore_expense",
                "undo_expense_change",
                "update_trip_members",
                "settle_up",
                "set_fx_rate",
                "start_trip",
                "update_trip",
                "end_trip",
                "activate_trip",
                "reopen_trip",
                "set_default_trip",
            )
        }

    async def purge_user(self, chat: ChatRef, user: UserRef, stores: Stores) -> None:
        logger.info("expense purge_user no-op chat=%s user=%s", chat.key, user.key)

    async def purge_chat(self, chat: ChatRef, stores: Stores) -> None:
        await self._ledger(stores).purge_chat(chat)


class _CommitHandler(PendingActionHandler):
    def __init__(self, plugin: ExpensesPlugin) -> None:
        self.plugin = plugin

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        ledger = self.plugin._ledger(ctx.stores)
        trip = None
        if action.kind == "end_trip":
            trip = await ledger.trip(action.chat, action.payload["trip_id"])
            if trip is None:
                return "Not saved: trip not found in this chat."
        try:
            result = await ledger.commit(
                action.chat, action.kind, action.payload, actor=ctx.user.ref, admin=ctx.is_admin
            )
        except (ValueError, PermissionError) as exc:
            return f"Not saved: {exc}"
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                return "Not saved: a trip with that name is already planned or active in this chat."
            raise
        if action.kind in {"add_expense", "update_expense", "delete_expense", "restore_expense"}:
            row = await ledger.get_expense(
                action.chat, action.payload["expense_id"], include_deleted=True
            )
            if row is not None:
                return await self._expense_echo(action, ctx, ledger, row)
        if action.kind == "update_trip_members":
            trip = await ledger.trip(action.chat, action.payload["trip_id"])
            if trip is not None:
                users = {
                    user.ref.key: user.display_name
                    for user in await ctx.stores.repo.find_members(action.chat)
                }
                names = [users.get(key, key) for key in trip.get("members", [])]
                trip_name = trip["name"]
                if not trip_name.casefold().endswith(" trip"):
                    trip_name += " trip"
                added = [users.get(key, key) for key in result.get("added", [])]
                removed = [users.get(key, key) for key in result.get("removed", [])]
                details = []
                if added:
                    details.append("added " + ", ".join(added))
                if removed:
                    details.append("removed " + ", ".join(removed))
                suffix = f" ({'; '.join(details)})." if details else " (no changes)."
                return f"{trip_name} members: {', '.join(names) or 'none set'}{suffix}"
        if action.kind == "undo_expense_change":
            return result
        if action.kind == "end_trip" and trip is not None:
            tool_ctx = ToolContext(
                action.chat,
                ctx.user,
                False,
                ctx.platform,
                ctx.stores,
                thread_id=ctx.thread_id,
                is_admin=ctx.is_admin,
            )
            current = await self.plugin._balance_text(tool_ctx, ledger, action.chat, trip)
            return f"Trip ended: {trip['name']}\nFinal settle-up summary:\n{current}"
        return result

    async def _expense_echo(self, action, ctx, ledger, row: dict) -> str:
        snapshot = _expense_snapshot(row)
        expected = action.payload.get("expected") or {}
        users = _member_labels(
            await ctx.stores.repo.find_members(action.chat),
            [
                snapshot["paid_by"],
                *snapshot["shares"],
                *([expected["paid_by"], *expected["shares"]] if expected else []),
            ],
        )
        trip = await ledger.trip(action.chat, snapshot["trip_id"]) if snapshot["trip_id"] else None
        trip_label = f"({trip['name']})" if trip else "(general ledger)"
        amount = format_amount(snapshot["amount"], snapshot["currency"])
        if action.kind == "delete_expense":
            return (
                f"Deleted: {snapshot['description']} {amount} {trip_label}. "
                'Say "undo" to bring it back.'
            )
        if action.kind == "restore_expense":
            return (
                f'Restored: {snapshot["description"]} {amount} {trip_label}. Say "undo" to revert.'
            )
        if action.kind == "add_expense":
            shares = snapshot["shares"]
            if action.payload.get("split_type") == "equal":
                split_text = f"split {len(shares)} ways: {_member_names(shares, users)}"
            else:
                split_text = "split: " + ", ".join(
                    f"{users.get(key, key)} {format_amount(value, snapshot['currency'])}"
                    for key, value in shares.items()
                )
            text = (
                f"Added: {snapshot['description']} {amount}, paid by "
                f"{users.get(snapshot['paid_by'], snapshot['paid_by'])}, {split_text} "
                f'{trip_label}. Say "undo" to revert.'
            )
            if (
                trip
                and trip["settle_mode"] == "convert"
                and snapshot["currency"].upper() != trip["settle_currency"].upper()
                and snapshot["fx_rate_to_settle"] is None
            ):
                text += " FX: no rate found yet; set one with set_fx_rate."
            return text
        changed = []
        if expected["description"] != snapshot["description"]:
            changed.append(f"description {expected['description']} → {snapshot['description']}")
        if (expected["amount"], expected["currency"]) != (snapshot["amount"], snapshot["currency"]):
            changed.append(
                f"amount {format_amount(expected['amount'], expected['currency'])} → {amount}"
            )
        if expected["paid_by"] != snapshot["paid_by"]:
            changed.append(
                f"paid by {users.get(expected['paid_by'], expected['paid_by'])} → "
                f"{users.get(snapshot['paid_by'], snapshot['paid_by'])}"
            )
        if expected["shares"] != snapshot["shares"]:
            changed.append(_split_change_text(expected["shares"], snapshot["shares"], users))
        if expected["spent_on"] != snapshot["spent_on"]:
            changed.append(f"date {expected['spent_on']} → {snapshot['spent_on']}")
        if expected["trip_id"] != snapshot["trip_id"]:
            old_trip = (
                await ledger.trip(action.chat, expected["trip_id"]) if expected["trip_id"] else None
            )
            changed.append(
                f"trip {old_trip['name'] if old_trip else 'general ledger'} → "
                f"{trip['name'] if trip else 'general ledger'}"
            )
        return (
            f"Updated: {snapshot['description']} {trip_label}: "
            + "; ".join(changed)
            + '. Say "undo" to revert.'
        )
