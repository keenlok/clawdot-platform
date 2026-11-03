"""Pure CSV export construction for one expense ledger scope."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, tzinfo
from decimal import Decimal
from typing import Any

from .balances import ledger_balances, simplify_balances
from .currency import currency_exponent

_HEADER = (
    "record",
    "trip_id",
    "trip_name",
    "trip_status",
    "trip_start_date",
    "trip_end_date",
    "id",
    "date",
    "description",
    "person",
    "counterparty",
    "currency",
    "amount",
    "fx_rate",
    "settle_currency",
    "amount_in_settle",
    "note",
)


@dataclass(frozen=True, slots=True)
class LedgerExport:
    filename: str
    data: bytes
    expense_count: int
    settlement_count: int


def _text(value: str) -> str:
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


def _major(minor: int, currency: str) -> str:
    exponent = currency_exponent(currency)
    return f"{Decimal(minor) / (10**exponent):.{exponent}f}"


def _settle_major(minor: int, currency: str, rate: Any, settle_currency: str) -> str:
    source_exp = currency_exponent(currency)
    settle_exp = currency_exponent(settle_currency)
    converted = Decimal(minor) * Decimal(str(rate)) * (Decimal(10) ** (settle_exp - source_exp))
    value = converted / (Decimal(10) ** settle_exp)
    result = format(value.normalize(), "f")
    if settle_exp:
        decimals = len(result.partition(".")[2])
        if decimals < settle_exp:
            result += ("." if "." not in result else "") + "0" * (settle_exp - decimals)
    return result


def _date(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else date.fromisoformat(value).isoformat()


def _rate(value: Any) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def export_caption(
    trip: Mapping[str, Any] | None, expense_count: int, settlement_count: int
) -> str:
    name = trip["name"] if trip else "General ledger"
    if trip:
        name += f" ({trip.get('status', 'active')})"
        if trip.get("start_date") or trip.get("end_date"):
            name += f"; {trip.get('start_date') or 'not set'} → {trip.get('end_date') or 'not set'}"
    return (
        f"{name}: {expense_count} expenses, {settlement_count} settlements. "
        "Deleted expenses are not included."
    )


def build_export(
    trip: Mapping[str, Any] | None,
    expenses: Sequence[Mapping[str, Any]],
    settlements: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
    *,
    today: date,
    timezone: tzinfo,
) -> LedgerExport:
    trip_id = str(trip["id"]) if trip else ""
    trip_name = str(trip["name"]) if trip else "General ledger"
    all_keys = {e["paid_by"] for e in expenses} | {
        s["user_key"] for e in expenses for s in e["shares"]
    }
    all_keys |= {s[key] for s in settlements for key in ("from_user", "to_user")}
    unknown = {
        key: f"Former member {index}"
        for index, key in enumerate(sorted(all_keys - names.keys()), 1)
    }

    def display_name(key: str) -> str:
        return names[key] if key in names else unknown[key]

    def person(key: str) -> str:
        return _text(display_name(key))

    def base(record: str) -> dict[str, str]:
        return {
            "record": record,
            "trip_id": trip_id,
            "trip_name": _text(trip_name),
            "trip_status": str(trip.get("status", "active")) if trip else "",
            "trip_start_date": _date(trip["start_date"]) if trip and trip.get("start_date") else "",
            "trip_end_date": _date(trip["end_date"]) if trip and trip.get("end_date") else "",
        }

    rows: list[dict[str, str]] = []
    ordered_expenses = sorted(
        expenses, key=lambda e: (_date(e["spent_on"]), e["created_at"], e["id"])
    )
    convert = bool(trip and trip["settle_mode"] == "convert")
    settle_currency = trip["settle_currency"] if convert else ""
    for expense in ordered_expenses:
        rate = expense.get("fx_rate_to_settle") if convert else None
        row = base("expense")
        row.update(
            id=str(expense["id"]),
            date=_date(expense["spent_on"]),
            description=_text(str(expense["description"])),
            person=person(expense["paid_by"]),
            currency=expense["currency"],
            amount=_major(int(expense["amount"]), expense["currency"]),
            fx_rate=_rate(rate) if rate is not None else "",
            settle_currency=settle_currency if rate is not None else "",
            amount_in_settle=_settle_major(
                int(expense["amount"]), expense["currency"], rate, settle_currency
            )
            if rate is not None
            else "",
            note="no FX rate" if convert and rate is None else "",
        )
        rows.append(row)
        ordered_shares = sorted(
            expense["shares"], key=lambda s: (display_name(s["user_key"]), s["user_key"])
        )
        for share in ordered_shares:
            share_row = base("share")
            share_row.update(
                id=str(expense["id"]),
                date=_date(expense["spent_on"]),
                description=_text(str(expense["description"])),
                person=person(share["user_key"]),
                currency=expense["currency"],
                amount=_major(int(share["amount"]), expense["currency"]),
                fx_rate=_rate(rate) if rate is not None else "",
                settle_currency=settle_currency if rate is not None else "",
                amount_in_settle=_settle_major(
                    int(share["amount"]), expense["currency"], rate, settle_currency
                )
                if rate is not None
                else "",
            )
            rows.append(share_row)

    for settlement in sorted(settlements, key=lambda s: (s["created_at"], s["id"])):
        settlement_row = base("settlement")
        settlement_row.update(
            id=str(settlement["id"]),
            date=settlement["created_at"].astimezone(timezone).date().isoformat(),
            person=person(settlement["from_user"]),
            counterparty=person(settlement["to_user"]),
            currency=settlement["currency"],
            amount=_major(int(settlement["amount"]), settlement["currency"]),
            note=(
                "not in settle currency; excluded from balance"
                if convert and settlement["currency"] != settle_currency
                else ""
            ),
        )
        rows.append(settlement_row)

    groups, unconverted = ledger_balances(trip, expenses, settlements)
    for currency, values in sorted(groups.items()):
        for key, amount in sorted(
            values.items(), key=lambda item: (display_name(item[0]), item[0])
        ):
            balance_row = base("balance")
            balance_row.update(
                person=person(key), currency=currency, amount=_major(amount, currency)
            )
            rows.append(balance_row)
    for currency, values in sorted(groups.items()):
        nonzero = {key: amount for key, amount in values.items() if amount}
        for source, target, amount in simplify_balances(nonzero):
            transfer_row = base("transfer")
            transfer_row.update(
                person=person(source),
                counterparty=person(target),
                currency=currency,
                amount=_major(amount, currency),
            )
            rows.append(transfer_row)
    for expense in sorted(
        unconverted, key=lambda e: (_date(e["spent_on"]), e["created_at"], e["id"])
    ):
        no_rate_row = base("no_fx_rate")
        no_rate_row.update(
            id=str(expense["id"]),
            date=_date(expense["spent_on"]),
            description=_text(str(expense["description"])),
            person=person(expense["paid_by"]),
            currency=expense["currency"],
            amount=_major(int(expense["amount"]), expense["currency"]),
            note="excluded from balance until set_fx_rate",
        )
        rows.append(no_rate_row)

    slug = "general"
    if trip:
        slug = (
            re.sub(r"[^a-z0-9]+", "-", str(trip["name"]).lower()).strip("-")[:40].strip("-")
            or "trip"
        )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_HEADER)
    writer.writeheader()
    writer.writerows(rows)
    return LedgerExport(
        f"expenses-{slug}-{today.isoformat()}.csv",
        output.getvalue().encode("utf-8-sig"),
        len(expenses),
        len(settlements),
    )
