"""Currency-aware ledger balance and settlement calculations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from .currency import currency_exponent


def net_balances(
    expenses: Iterable[Mapping[str, Any]], settlements: Iterable[Mapping[str, Any]] = ()
) -> dict[str, dict[str, int]]:
    """Return net minor units per user and currency; positive means owed money."""
    balances: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for expense in expenses:
        currency = expense["currency"]
        amount = int(expense["amount"])
        balances[expense["paid_by"]][currency] += amount
        for share in expense["shares"]:
            balances[share["user_key"]][currency] -= int(share["amount"])
    for settlement in settlements:
        currency = settlement["currency"]
        amount = int(settlement["amount"])
        balances[settlement["from_user"]][currency] += amount
        balances[settlement["to_user"]][currency] -= amount
    return {user: dict(currencies) for user, currencies in balances.items()}


def ledger_balances(
    trip: Mapping[str, Any] | None,
    expenses: Iterable[Mapping[str, Any]],
    settlements: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, int]], list[Mapping[str, Any]]]:
    """Per-currency net balances for a ledger scope, plus convert-trip expenses with no rate."""
    expenses = list(expenses)
    settlements = list(settlements)
    if trip and trip["settle_mode"] == "convert":
        net, unconverted = convert_balances(expenses, settlements, trip["settle_currency"])
        groups = {trip["settle_currency"]: net}
    else:
        balances = net_balances(expenses, settlements)
        codes = {e["currency"] for e in expenses} | {s["currency"] for s in settlements}
        groups = {
            code: {user: values.get(code, 0) for user, values in balances.items()} for code in codes
        }
        unconverted = []
    return groups, unconverted


def convert_balances(
    expenses: Iterable[Mapping[str, Any]],
    settlements: Iterable[Mapping[str, Any]],
    settle_currency: str,
) -> tuple[dict[str, int], list[Mapping[str, Any]]]:
    """Convert expense contributions with stored rates and round aggregate balances half-even."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    unconverted = []
    for expense in expenses:
        rate = expense.get("fx_rate_to_settle")
        if rate is None:
            unconverted.append(expense)
            continue
        scale = Decimal(10) ** (
            currency_exponent(settle_currency) - currency_exponent(expense["currency"])
        )
        paid = Decimal(int(expense["amount"])) * Decimal(str(rate)) * scale
        totals[expense["paid_by"]] += paid
        for share in expense["shares"]:
            totals[share["user_key"]] -= Decimal(int(share["amount"])) * Decimal(str(rate)) * scale
    for settlement in settlements:
        if settlement["currency"] != settle_currency:
            continue
        amt = int(settlement["amount"])
        totals[settlement["from_user"]] += amt
        totals[settlement["to_user"]] -= amt
    rounded = {
        user: int(value.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
        for user, value in totals.items()
    }
    # Independent half-even rounding can introduce a one-unit group imbalance; allocate that
    # aggregate remainder to the account with the largest rounding error, deterministically.
    remainder = sum(rounded.values())
    while remainder:
        errors = {user: Decimal(rounded[user]) - exact for user, exact in totals.items()}
        if remainder > 0:
            user = max(errors, key=lambda key: (errors[key], key))
            rounded[user] -= 1
            remainder -= 1
        else:
            user = min(errors, key=lambda key: (errors[key], key))
            rounded[user] += 1
            remainder += 1
    return rounded, unconverted


def simplify_balances(balances: Mapping[str, int]) -> list[tuple[str, str, int]]:
    """Match largest debtor to largest creditor until all balances are settled."""
    debtors = [[u, -int(v)] for u, v in balances.items() if v < 0]
    creditors = [[u, int(v)] for u, v in balances.items() if v > 0]
    if sum(v for _, v in debtors) != sum(v for _, v in creditors):
        raise ValueError("Balances must net to zero.")
    transfers = []
    while debtors and creditors:
        debtor = max(debtors, key=lambda item: (item[1], item[0]))
        creditor = max(creditors, key=lambda item: (item[1], item[0]))
        amount = min(debtor[1], creditor[1])
        transfers.append((debtor[0], creditor[0], amount))
        debtor[1] -= amount
        creditor[1] -= amount
        if debtor[1] == 0:
            debtors.remove(debtor)
        if creditor[1] == 0:
            creditors.remove(creditor)
    return transfers
