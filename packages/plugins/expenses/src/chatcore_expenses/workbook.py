"""Pure, formatted multi-tab workbook model for one expense ledger scope.

The model is plain JSON so any spreadsheet renderer can consume it without importing this
plugin. Contract (``schema`` 1)::

    {"schema": 1, "title": str, "sheets": [sheet, ...]}
    sheet = {"title": str, "rows": [[cell | None, ...], ...], "widths": [int, ...],
             "frozen_rows": int, "tables": [table, ...]}
    table = {"header": row, "first": row, "last": row, "columns": int, "filter": bool}
    cell  = {"k": kind, "v": value, "s": style (optional), "exp": int (money and sum only)}

Row indices are 0-based. Kinds: ``text`` (literal string, never a formula), ``number`` and
``rate`` (exact decimal string), ``money`` (exact decimal string shown with ``exp`` decimals),
``date`` (``YYYY-MM-DD``), ``datetime`` (local wall time ``YYYY-MM-DDTHH:MM:SS``) and ``sum``
(``v`` = ``[first_row, last_row]``: the sum of the same column over those rows). Styles:
``title``, ``note``, ``warn``, ``header``, ``total``, ``wrap``, ``pos``, ``neg``.

Every figure is computed here from ledger rows. Balances, conversion and rounding come from
``balances.py`` (``ledger_balances``/``simplify_balances``), so the workbook agrees with the
balance tools, including the aggregate group rounding adjustment.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from .balances import ledger_balances, simplify_balances
from .currency import currency_exponent

SCHEMA = 1
FIXED_TABS = ("Summary", "Transactions", "Repayments", "Raw ledger")
MAX_PARTICIPANT_TABS = 60
_TAB_UNSAFE = re.compile(r"['\[\]*?/\\:]")
SNAPSHOT_NOTE = (
    "Snapshot only: this file does not update when the ledger changes. "
    "Export again for current figures."
)


@dataclass(frozen=True, slots=True)
class Workbook:
    model: dict
    preview: str
    expense_count: int
    settlement_count: int
    snapshot: str


# --- cells -----------------------------------------------------------------------------------


def _dec(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def text(value: Any, style: str | None = None) -> dict:
    cell = {"k": "text", "v": str(value)}
    if style:
        cell["s"] = style
    return cell


def money(value: Decimal | None, exp: int, style: str | None = None) -> dict | None:
    if value is None:
        return None
    cell = {"k": "money", "v": _dec(value), "exp": exp}
    if style:
        cell["s"] = style
    return cell


def number(value: int | Decimal, style: str | None = None) -> dict:
    cell = {"k": "number", "v": _dec(Decimal(value))}
    if style:
        cell["s"] = style
    return cell


def rate(value: Decimal | None) -> dict | None:
    return None if value is None else {"k": "rate", "v": _dec(value)}


def day(value: date | str | None) -> dict | None:
    if value is None or value == "":
        return None
    return {"k": "date", "v": _date(value).isoformat()}


def moment(value: datetime, timezone: tzinfo) -> dict:
    local = value.astimezone(timezone).replace(tzinfo=None, microsecond=0)
    return {"k": "datetime", "v": local.isoformat()}


def total(first: int, last: int, exp: int) -> dict:
    return {"k": "sum", "v": [first, last], "exp": exp, "s": "total"}


def _date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _major(minor: int, currency: str) -> Decimal:
    return Decimal(int(minor)) / (Decimal(10) ** currency_exponent(currency))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _position(net: int) -> dict:
    if net > 0:
        return text("Owed to you", "pos")
    if net < 0:
        return text("You owe", "neg")
    return text("Settled")


# --- names -----------------------------------------------------------------------------------


def _labels(keys: set[str], names: Mapping[str, str]) -> dict[str, str]:
    """Unique readable label per stable user key.

    Keys with no current member become "Former member N" (as in the CSV export); members whose
    display name is blank become "Unnamed member N". Repeated names get " (2)", " (3)".
    """
    unknown = {
        key: f"Former member {index}" for index, key in enumerate(sorted(keys - names.keys()), 1)
    }
    blank = sorted(key for key in keys if key in names and not " ".join(names[key].split()))
    unnamed = {key: f"Unnamed member {index}" for index, key in enumerate(blank, 1)}
    base = {
        key: unknown.get(key) or unnamed.get(key) or " ".join(names[key].split()) for key in keys
    }
    labels: dict[str, str] = {}
    seen: dict[str, int] = defaultdict(int)
    for key in sorted(keys, key=lambda k: (base[k].casefold(), k)):
        folded = base[key].casefold()
        seen[folded] += 1
        labels[key] = base[key] if seen[folded] == 1 else f"{base[key]} ({seen[folded]})"
    return labels


def tab_names(labels: Sequence[str]) -> list[str]:
    """Safe, unique (case-insensitive) sheet titles for participant labels, in order."""
    used = {name.casefold() for name in FIXED_TABS}
    result = []
    for index, label in enumerate(labels, 1):
        cleaned = "".join(
            " " if unicodedata.category(char).startswith(("C", "Z")) else char for char in label
        )
        cleaned = " ".join(_TAB_UNSAFE.sub(" ", cleaned).split())[:50].strip()
        cleaned = cleaned or f"Person {index}"
        candidate, counter = cleaned, 1
        while candidate.casefold() in used:
            counter += 1
            candidate = f"{cleaned[:44]} ({counter})"
        used.add(candidate.casefold())
        result.append(candidate)
    return result


# --- builder ---------------------------------------------------------------------------------


class _Sheet:
    def __init__(self, title: str) -> None:
        self.title = title
        self.rows: list[list[dict | None]] = []
        self.tables: list[dict] = []
        self.widths: list[int] = []
        self.frozen_rows = 0

    def add(self, *cells: dict | None) -> int:
        self.rows.append(list(cells))
        return len(self.rows) - 1

    def blank(self, count: int = 1) -> None:
        for _ in range(count):
            self.rows.append([])

    def header(self, labels: Sequence[str]) -> int:
        return self.add(*(text(label, "header") for label in labels))

    def table(self, header: int, last: int, columns: int, *, filter: bool = False) -> None:
        self.tables.append(
            {
                "header": header,
                "first": header + 1,
                "last": last,
                "columns": columns,
                "filter": filter,
            }
        )

    def model(self) -> dict:
        return {
            "title": self.title,
            "rows": self.rows,
            "widths": self.widths,
            "frozen_rows": self.frozen_rows,
            "tables": self.tables,
        }


def build_workbook(
    trip: Mapping[str, Any] | None,
    expenses: Sequence[Mapping[str, Any]],
    settlements: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
    *,
    now: datetime,
    timezone: tzinfo,
) -> Workbook:
    """Build the workbook model; raises ``ValueError`` when it would exceed tab limits."""
    convert = bool(trip and trip.get("settle_mode") == "convert")
    settle = str(trip["settle_currency"]) if convert else ""
    sexp = currency_exponent(settle) if convert else 0
    trip_name = str(trip["name"]) if trip else "General ledger"

    keys = {e["paid_by"] for e in expenses} | {s["user_key"] for e in expenses for s in e["shares"]}
    keys |= {s[k] for s in settlements for k in ("from_user", "to_user")}
    labels = _labels(keys, names)
    people = sorted(keys, key=lambda k: (labels[k].casefold(), k))
    if len(people) > MAX_PARTICIPANT_TABS:
        raise ValueError(
            f"This ledger has {len(people)} participants; the workbook supports at most "
            f"{MAX_PARTICIPANT_TABS} person tabs."
        )
    tabs = dict(zip(people, tab_names([labels[k] for k in people]), strict=True))

    ordered = sorted(expenses, key=lambda e: (_date(e["spent_on"]), e["created_at"], str(e["id"])))
    ordered_settlements = sorted(settlements, key=lambda s: (s["created_at"], str(s["id"])))
    currencies = sorted({e["currency"] for e in expenses} | {s["currency"] for s in settlements})

    def fx(expense: Mapping[str, Any]) -> Decimal | None:
        value = expense.get("fx_rate_to_settle") if convert else None
        return Decimal(str(value)) if value is not None else None

    def in_settle(minor: int, expense: Mapping[str, Any]) -> Decimal | None:
        value = fx(expense)
        return None if value is None else _major(minor, expense["currency"]) * value

    groups, unconverted = ledger_balances(trip, expenses, settlements)
    missing_ids = {str(e["id"]) for e in unconverted}

    # Per-person aggregates. Settlement-currency figures are exact (unrounded) sums.
    paid_s: dict[str, Decimal] = defaultdict(Decimal)
    share_s: dict[str, Decimal] = defaultdict(Decimal)
    paid_orig: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    share_orig: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for expense in ordered:
        currency = expense["currency"]
        paid_orig[expense["paid_by"], currency] += _major(expense["amount"], currency)
        converted = in_settle(int(expense["amount"]), expense)
        if converted is not None:
            paid_s[expense["paid_by"]] += converted
        for share in expense["shares"]:
            share_orig[share["user_key"], currency] += _major(share["amount"], currency)
            converted = in_settle(int(share["amount"]), expense)
            if converted is not None:
                share_s[share["user_key"]] += converted
    sent: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    received: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for settlement in settlements:
        amount = _major(settlement["amount"], settlement["currency"])
        sent[settlement["from_user"], settlement["currency"]] += amount
        received[settlement["to_user"], settlement["currency"]] += amount

    def counted(settlement: Mapping[str, Any]) -> bool:
        return not convert or settlement["currency"] == settle

    def canonical(key: str, currency: str) -> int:
        return int(groups.get(currency, {}).get(key, 0))

    def rounding_adjustment(key: str) -> Decimal:
        exact = paid_s[key] - share_s[key] + sent[key, settle] - received[key, settle]
        rounded = (exact * (Decimal(10) ** sexp)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        return (Decimal(canonical(key, settle)) - rounded) / (Decimal(10) ** sexp)

    snapshot_text = now.astimezone(timezone).strftime("%d %b %Y %H:%M") + f" ({timezone})"
    dates = ""
    if trip and (trip.get("start_date") or trip.get("end_date")):
        dates = (
            f"{_date(trip['start_date']).isoformat() if trip.get('start_date') else 'not set'}"
            f" to {_date(trip['end_date']).isoformat() if trip.get('end_date') else 'not set'}"
        )
    counts = (
        f"{_plural(len(expenses), 'expense')} · {_plural(len(settlements), 'recorded repayment')}"
    )
    missing_note = None
    if unconverted:
        by_currency: dict[str, Decimal] = defaultdict(Decimal)
        for expense in unconverted:
            by_currency[expense["currency"]] += _major(expense["amount"], expense["currency"])
        listed = ", ".join(
            f"{code} {value:,.{currency_exponent(code)}f}"
            for code, value in sorted(by_currency.items())
        )
        missing_note = (
            f"{_plural(len(unconverted), 'expense')} "
            f"{'has' if len(unconverted) == 1 else 'have'} no stored FX rate to {settle} "
            f"({listed}). "
            f"{settle} figures and balances exclude them until a rate is set; "
            "original-currency totals include them."
        )

    # --- Summary ---------------------------------------------------------------------------
    summary = _Summary(
        trip, trip_name, dates, now, timezone, convert, settle, expenses, settlements
    )
    sheet = summary.sheet
    if missing_note:
        sheet.add(text(missing_note, "warn"))
        sheet.blank()
    if convert:
        orig_cols = [(c, "Paid") for c in currencies] + [(c, "Share") for c in currencies]
        header = sheet.header(
            [
                "Person",
                f"Paid upfront {settle}",
                f"Share {settle}",
                f"Repaid {settle}",
                f"Received {settle}",
                f"Rounding {settle}",
                f"Net balance {settle}",
                "Position",
            ]
            + [f"{kind} {code} (original)" for code, kind in orig_cols]
        )
        for key in people:
            sheet.add(
                text(labels[key]),
                money(paid_s[key], sexp),
                money(share_s[key], sexp),
                money(sent[key, settle], sexp),
                money(received[key, settle], sexp),
                money(rounding_adjustment(key), sexp),
                money(_major(canonical(key, settle), settle), sexp),
                _position(canonical(key, settle)),
                *(
                    money(
                        (paid_orig if kind == "Paid" else share_orig)[key, code],
                        currency_exponent(code),
                    )
                    for code, kind in orig_cols
                ),
            )
        last = len(sheet.rows) - 1
        if people:
            sheet.add(
                text("Total", "total"),
                *(total(header + 1, last, sexp) for _ in range(6)),
                text("", "total"),
                *(total(header + 1, last, currency_exponent(code)) for code, _ in orig_cols),
            )
        sheet.table(header, len(sheet.rows) - 1, 8 + len(orig_cols))
        sheet.widths = [160, 110, 110, 100, 100, 100, 120, 110] + [120] * len(orig_cols)
    else:
        sheet.widths = [160, 120, 120, 120, 120, 120, 110]
        if not currencies:
            sheet.add(text("No expenses or repayments in this ledger.", "note"))
        for code in currencies:
            exp = currency_exponent(code)
            header = sheet.header(
                [
                    "Person",
                    f"Paid upfront {code}",
                    f"Share {code}",
                    f"Repaid {code}",
                    f"Received {code}",
                    f"Net balance {code}",
                    "Position",
                ]
            )
            active = [
                k
                for k in people
                if paid_orig[k, code]
                or share_orig[k, code]
                or sent[k, code]
                or received[k, code]
                or canonical(k, code)
            ]
            for key in active:
                sheet.add(
                    text(labels[key]),
                    money(paid_orig[key, code], exp),
                    money(share_orig[key, code], exp),
                    money(sent[key, code], exp),
                    money(received[key, code], exp),
                    money(_major(canonical(key, code), code), exp),
                    _position(canonical(key, code)),
                )
            last = len(sheet.rows) - 1
            if active:
                sheet.add(
                    text("Total", "total"),
                    *(total(header + 1, last, exp) for _ in range(5)),
                    text("", "total"),
                )
            sheet.table(header, len(sheet.rows) - 1, 7)
            sheet.blank()
    sheet.blank()
    notes = [
        "Positive net balance: owed to you. Negative: you owe. Each row is from that "
        "person's point of view.",
        "Net balance = paid upfront − share + repaid − received.",
    ]
    if convert:
        notes += [
            f"{settle} values use each expense's stored FX rate; display rounding does not change "
            "calculation precision.",
            "Net balances use the bot's aggregate rounding; the Rounding column shows any "
            "one-unit group adjustment.",
            f"Repayments not recorded in {settle} are listed on Repayments but are not counted "
            "in these balances.",
        ]
    notes += [
        "Suggested transfers on Repayments are not payments already made.",
        "Deleted expenses are excluded. " + SNAPSHOT_NOTE,
    ]
    for note in notes:
        sheet.add(text(note, "note"))

    # --- Transactions ----------------------------------------------------------------------
    tx = _Sheet("Transactions")
    tx.add(text(f"{trip_name} — transactions", "title"))
    tx.add(
        text(
            f"{_plural(len(expenses), 'expense')} · sorted by date, then time entered · "
            f"snapshot {snapshot_text}",
            "note",
        )
    )
    tx.add(
        text(
            (
                "Share columns show the original currency, then " + settle + " at the stored rate. "
                if convert
                else "Amounts are in each expense's original currency. "
            )
            + SNAPSHOT_NOTE,
            "note",
        )
    )
    tx.blank()
    columns = ["Date", "Description", "Paid by", "Currency", "Amount (original)"]
    if convert:
        columns += [f"FX rate to {settle}", f"Amount {settle}", "FX status"]
    for key in people:
        columns.append(f"{labels[key]} share (original)")
        if convert:
            columns.append(f"{labels[key]} share {settle}")
    header = tx.header(columns)
    tx.frozen_rows = header + 1
    for expense in ordered:
        currency, exp = expense["currency"], currency_exponent(expense["currency"])
        shares = {s["user_key"]: int(s["amount"]) for s in expense["shares"]}
        row = [
            day(expense["spent_on"]),
            text(expense["description"], "wrap"),
            text(labels[expense["paid_by"]]),
            text(currency),
            money(_major(expense["amount"], currency), exp),
        ]
        if convert:
            row += [
                rate(fx(expense)),
                money(in_settle(int(expense["amount"]), expense), sexp),
                text("Missing FX rate", "warn")
                if str(expense["id"]) in missing_ids
                else text("Stored rate"),
            ]
        for key in people:
            amount = shares.get(key)
            row.append(money(_major(amount, currency), exp) if amount is not None else None)
            if convert:
                row.append(money(in_settle(amount, expense), sexp) if amount is not None else None)
        tx.add(*row)
    last = len(tx.rows) - 1
    if convert and ordered:
        label = f"Total {settle}" + (
            f" (excludes {len(unconverted)} without FX rate)" if unconverted else ""
        )
        cells: list[dict | None] = [
            None,
            text(label, "total"),
            None,
            None,
            None,
            None,
            total(header + 1, last, sexp),
            None,
        ]
        for _ in people:
            cells += [None, total(header + 1, last, sexp)]
        tx.add(*cells)
    tx.table(header, last, len(columns), filter=True)
    tx.widths = (
        [100, 260, 120, 70, 110]
        + ([110, 110, 120] if convert else [])
        + [110 for _ in range(len(columns) - (8 if convert else 5))]
    )

    # --- Repayments ------------------------------------------------------------------------
    rp = _Sheet("Repayments")
    rp.add(text("Suggested repayments", "title"))
    rp.add(text(f"Calculated from this snapshot's balances · {snapshot_text}", "note"))
    rp.add(
        text(
            "Suggested transfers are recommendations, not payments already made. "
            "Record real repayments in the chat.",
            "note",
        )
    )
    rp.blank()
    header = rp.header(["From", "To", "Currency", "Suggested amount", "Status"])
    suggestions = 0
    for code, values in sorted(groups.items()):
        for source, target, amount in simplify_balances({k: v for k, v in values.items() if v}):
            rp.add(
                text(labels[source]),
                text(labels[target]),
                text(code),
                money(_major(amount, code), currency_exponent(code)),
                text("Suggested, not recorded"),
            )
            suggestions += 1
    if not suggestions:
        rp.add(text("Everyone is settled; no transfers are suggested.", "note"))
    rp.table(header, len(rp.rows) - 1, 5)
    rp.blank(2)
    rp.add(text("Recorded repayments", "title"))
    rp.add(text("Actual repayments recorded in the ledger at this snapshot.", "note"))
    rp.blank()
    header = rp.header(
        ["Date", "Recorded at", "From", "To", "Currency", "Amount", "Counted in balances"]
    )
    for settlement in ordered_settlements:
        rp.add(*_settlement_row(settlement, labels, timezone, counted(settlement), settle))
    if not ordered_settlements:
        rp.add(text("No repayments recorded at this snapshot.", "note"))
    rp.table(header, len(rp.rows) - 1, 7, filter=bool(ordered_settlements))
    rp.widths = [100, 150, 140, 140, 80, 120, 200]

    # --- Person tabs -----------------------------------------------------------------------
    person_sheets = []
    for key in people:
        ps = _Sheet(tabs[key])
        mine = [
            e
            for e in ordered
            if e["paid_by"] == key or any(s["user_key"] == key for s in e["shares"])
        ]
        ps.add(text(f"{labels[key]} — personal breakdown", "title"))
        ps.add(
            text(
                f"{_plural(len(mine), 'relevant expense')} · {trip_name} · "
                f"snapshot {snapshot_text}",
                "note",
            )
        )
        ps.add(text("Positive net: owed to you. Negative: you owe. " + SNAPSHOT_NOTE, "note"))
        ps.blank()
        if convert:
            header = ps.header(
                [
                    f"Paid upfront {settle}",
                    f"Share {settle}",
                    f"Repaid {settle}",
                    f"Received {settle}",
                    f"Rounding {settle}",
                    f"Net balance {settle}",
                    "Position",
                ]
            )
            ps.add(
                money(paid_s[key], sexp),
                money(share_s[key], sexp),
                money(sent[key, settle], sexp),
                money(received[key, settle], sexp),
                money(rounding_adjustment(key), sexp),
                money(_major(canonical(key, settle), settle), sexp),
                _position(canonical(key, settle)),
            )
            ps.table(header, len(ps.rows) - 1, 7)
        else:
            header = ps.header(
                [
                    "Currency",
                    "Paid upfront",
                    "Share",
                    "Repaid",
                    "Received",
                    "Net balance",
                    "Position",
                ]
            )
            for code in currencies:
                if not (
                    paid_orig[key, code]
                    or share_orig[key, code]
                    or sent[key, code]
                    or received[key, code]
                    or canonical(key, code)
                ):
                    continue
                exp = currency_exponent(code)
                ps.add(
                    text(code),
                    money(paid_orig[key, code], exp),
                    money(share_orig[key, code], exp),
                    money(sent[key, code], exp),
                    money(received[key, code], exp),
                    money(_major(canonical(key, code), code), exp),
                    _position(canonical(key, code)),
                )
            ps.table(header, len(ps.rows) - 1, 7)
        if convert and unconverted and any(str(e["id"]) in missing_ids for e in mine):
            ps.add(
                text(
                    f"Some expenses below have no FX rate and are excluded from {settle} figures.",
                    "warn",
                )
            )
        ps.blank()
        columns = [
            "Date",
            "Description",
            "Paid by",
            "Currency",
            "Full bill (original)",
            "Your share (original)",
            "You paid (original)",
        ]
        if convert:
            columns += [
                f"Full bill {settle}",
                f"Your share {settle}",
                f"You paid {settle}",
                f"Net effect {settle}",
                "FX status",
            ]
        header = ps.header(columns)
        for expense in mine:
            currency, exp = expense["currency"], currency_exponent(expense["currency"])
            share = sum(int(s["amount"]) for s in expense["shares"] if s["user_key"] == key)
            paid = int(expense["amount"]) if expense["paid_by"] == key else 0
            row = [
                day(expense["spent_on"]),
                text(expense["description"], "wrap"),
                text(labels[expense["paid_by"]]),
                text(currency),
                money(_major(expense["amount"], currency), exp),
                money(_major(share, currency), exp),
                money(_major(paid, currency), exp),
            ]
            if convert:
                bill, mine_s, paid_conv = (
                    in_settle(int(expense["amount"]), expense),
                    in_settle(share, expense),
                    in_settle(paid, expense),
                )
                row += [
                    money(bill, sexp),
                    money(mine_s, sexp),
                    money(paid_conv, sexp),
                    money(paid_conv - mine_s, sexp) if paid_conv is not None else None,
                    text("Missing FX rate", "warn")
                    if str(expense["id"]) in missing_ids
                    else text("Stored rate"),
                ]
            ps.add(*row)
        last = len(ps.rows) - 1
        if not mine:
            ps.add(text("No expenses paid or shared at this snapshot.", "note"))
            last = len(ps.rows) - 1
        elif convert:
            ps.add(
                None,
                text(f"Total {settle}" + (" (converted only)" if unconverted else ""), "total"),
                None,
                None,
                None,
                None,
                None,
                *(total(header + 1, last, sexp) for _ in range(4)),
                None,
            )
        ps.table(header, last, len(columns), filter=bool(mine))
        mine_settlements = [s for s in ordered_settlements if key in (s["from_user"], s["to_user"])]
        ps.blank()
        ps.add(text("Recorded repayments involving " + labels[key], "title"))
        header = ps.header(
            ["Date", "Recorded at", "From", "To", "Currency", "Amount", "Counted in balances"]
        )
        for settlement in mine_settlements:
            ps.add(*_settlement_row(settlement, labels, timezone, counted(settlement), settle))
        if not mine_settlements:
            ps.add(text("None recorded at this snapshot.", "note"))
        ps.table(header, len(ps.rows) - 1, 7)
        ps.widths = [100, 260, 120, 80, 120, 120, 120] + (
            [110, 110, 110, 110, 120] if convert else []
        )
        person_sheets.append(ps)

    # --- Raw ledger ------------------------------------------------------------------------
    raw = _Sheet("Raw ledger")
    raw.add(text("Raw ledger (audit)", "title"))
    raw.add(
        text(
            f"Every current expense, share and recorded repayment · snapshot "
            f"{snapshot_text}. Deleted expenses are excluded. IDs and user keys are "
            "stable references.",
            "note",
        )
    )
    raw.add(
        text(
            f"Scope: {trip_name}"
            + (f" ({trip.get('status', 'active')}, {trip.get('settle_mode')})" if trip else "")
            + (f" · ID {trip['id']}" if trip else ""),
            "note",
        )
    )
    raw.blank()
    header = raw.header(
        [
            "Record",
            "ID",
            "Date",
            "Recorded at",
            "Description",
            "User key",
            "Person",
            "Counterparty key",
            "Counterparty",
            "Currency",
            "Amount (minor units)",
            "Amount",
            "FX rate",
            "Settlement currency",
            "Settlement amount",
            "Note",
        ]
    )
    raw.frozen_rows = header + 1
    for expense in ordered:
        currency, exp = expense["currency"], currency_exponent(expense["currency"])
        value = fx(expense)
        note = []
        if str(expense["id"]) in missing_ids:
            note.append("no FX rate; excluded from settlement balance")
        if value is not None and Decimal(repr(float(value))) != value:
            note.append(f"exact FX rate {_dec(value)}")
        base = [
            day(expense["spent_on"]),
            moment(expense["created_at"], timezone),
            text(expense["description"], "wrap"),
        ]
        raw.add(
            text("expense"),
            text(expense["id"]),
            *base,
            text(expense["paid_by"]),
            text(labels[expense["paid_by"]]),
            None,
            None,
            text(currency),
            number(int(expense["amount"])),
            money(_major(expense["amount"], currency), exp),
            rate(value),
            text(settle) if value is not None else None,
            money(in_settle(int(expense["amount"]), expense), sexp),
            text("; ".join(note)) if note else None,
        )
        for share in sorted(
            expense["shares"], key=lambda s: (labels[s["user_key"]].casefold(), s["user_key"])
        ):
            raw.add(
                text("share"),
                text(expense["id"]),
                *base,
                text(share["user_key"]),
                text(labels[share["user_key"]]),
                text(expense["paid_by"]),
                text(labels[expense["paid_by"]]),
                text(currency),
                number(int(share["amount"])),
                money(_major(share["amount"], currency), exp),
                rate(value),
                text(settle) if value is not None else None,
                money(in_settle(int(share["amount"]), expense), sexp),
                None,
            )
    for settlement in ordered_settlements:
        currency = settlement["currency"]
        raw.add(
            text("repayment"),
            text(settlement["id"]),
            day(settlement["created_at"].astimezone(timezone).date()),
            moment(settlement["created_at"], timezone),
            None,
            text(settlement["from_user"]),
            text(labels[settlement["from_user"]]),
            text(settlement["to_user"]),
            text(labels[settlement["to_user"]]),
            text(currency),
            number(int(settlement["amount"])),
            money(_major(settlement["amount"], currency), currency_exponent(currency)),
            None,
            None,
            None,
            None if counted(settlement) else text(f"not in {settle}; excluded from balance"),
        )
    raw.table(header, len(raw.rows) - 1, 16, filter=True)
    raw.widths = [90, 280, 100, 150, 240, 180, 120, 180, 120, 80, 110, 110, 110, 90, 120, 240]

    sheets = [sheet, tx, rp, *person_sheets, raw]
    model = {
        "schema": SCHEMA,
        "title": "Expenses — " + trip_name[:150],
        "sheets": [s.model() for s in sheets],
    }

    preview_lines = [
        f"{trip_name}" + (f" ({dates})" if dates else "") + f": {counts}.",
        "Tabs: " + ", ".join(s.title for s in sheets) + ".",
    ]
    for code in [settle] if convert else currencies:
        exp = currency_exponent(code)
        balances = [
            f"{labels[k]} {'+' if canonical(k, code) > 0 else ''}"
            f"{_major(canonical(k, code), code):,.{exp}f}"
            for k in people
            if canonical(k, code)
        ]
        preview_lines.append(f"Net {code}: " + ("; ".join(balances) or "all settled") + ".")
    if missing_note:
        preview_lines.append(missing_note)
    preview = "\n".join(preview_lines)
    if len(preview) > 1500:
        preview = preview[:1497] + "..."
    return Workbook(model, preview, len(expenses), len(settlements), snapshot_text)


class _Summary:
    def __init__(
        self, trip, trip_name, dates, now, timezone, convert, settle, expenses, settlements
    ) -> None:
        sheet = _Sheet("Summary")
        sheet.add(text(f"{trip_name} — expense summary", "title"))
        sheet.blank()
        sheet.add(text("Trip"), text(trip_name))
        if trip:
            sheet.add(text("Status"), text(trip.get("status", "active")))
            sheet.add(text("Start date"), day(trip.get("start_date")))
            sheet.add(text("End date"), day(trip.get("end_date")))
        sheet.add(
            text("Settlement"),
            text(
                f"Converted to {settle} at each expense's stored FX rate"
                if convert
                else "Per currency (no conversion)"
            ),
        )
        sheet.add(text("Snapshot"), moment(now, timezone), text(str(timezone), "note"))
        sheet.add(text("Expenses"), number(len(expenses)))
        sheet.add(text("Recorded repayments"), number(len(settlements)))
        sheet.blank()
        self.sheet = sheet


def _settlement_row(settlement, labels, timezone, counted: bool, settle: str) -> list:
    currency = settlement["currency"]
    return [
        day(settlement["created_at"].astimezone(timezone).date()),
        moment(settlement["created_at"], timezone),
        text(labels[settlement["from_user"]]),
        text(labels[settlement["to_user"]]),
        text(currency),
        money(_major(settlement["amount"], currency), currency_exponent(currency)),
        text("Yes") if counted else text(f"No (not in {settle})", "warn"),
    ]
