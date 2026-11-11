"""Build expense summary card documents from ledger results."""

from decimal import Decimal

from chatcore.cards import CardError, validate_card

from .balances import simplify_balances
from .currency import currency_exponent, format_amount


def _plain_amount(minor: int, currency: str) -> str:
    exponent = currency_exponent(currency)
    amount = Decimal(minor) / (10**exponent)
    return f"{amount:,.{exponent}f}"


def _net_digits(minor: int, currency: str) -> str:
    return _plain_amount(abs(minor), currency)


def _settlement_block(transfers, names, currency):
    if len(transfers) > 40:
        raise CardError("invalid", "settlements: too many entries")
    if len(transfers) <= 12:
        return {
            "type": "bullets",
            "items": [
                f"{names.get(source, source)} pays {names.get(target, target)} "
                f"{format_amount(amount, currency)}"
                for source, target, amount in transfers
            ],
        }
    return {
        "type": "table",
        "columns": [
            {"header": "From"},
            {"header": "To"},
            {"header": f"Amount ({currency})", "align": "end"},
        ],
        "rows": [
            [names.get(source, source), names.get(target, target), _plain_amount(amount, currency)]
            for source, target, amount in transfers
        ],
    }


def balance_card(
    trip,
    totals,
    groups,
    names,
    *,
    currency_exponents=None,
    expenses=(),
    unconverted=(),
):
    """Build a card from the exact per-currency balances and unconverted rows."""
    title = "Balances · " + (trip["name"] if trip else "General ledger")
    tiles = [
        {"label": f"Spend {code}", "value": str(amount)}
        for code, amount in sorted(totals.items())[:3]
    ]
    tiles.append({"label": "Expenses", "value": str(len(expenses))})
    blocks = []
    if tiles:
        blocks.append({"type": "stats", "tiles": tiles[:4]})

    all_settled = not any(int(value) for values in groups.values() for value in values.values())
    for currency, values in sorted(groups.items()):
        if all_settled:
            continue
        nonzero = [(user, int(value)) for user, value in sorted(values.items()) if int(value)]
        rows = [
            [
                names.get(user, user),
                "is owed" if value > 0 else "owes",
                ("+" if value > 0 else "−") + _net_digits(value, currency),
            ]
            for user, value in nonzero
        ]
        settled = [user for user, value in values.items() if not int(value)]
        if settled:
            rows.append(["Everyone else", "settled", _net_digits(0, currency)])
        if len(rows) > 40:
            raise CardError("invalid", "table.rows: too many entries")
        if rows:
            blocks.append(
                {
                    "type": "table",
                    "columns": [
                        {"header": "Member"},
                        {"header": "Status"},
                        {"header": f"Net ({currency})", "align": "end"},
                    ],
                    "rows": rows,
                }
            )
        transfers = simplify_balances({user: value for user, value in values.items() if value})
        if transfers:
            blocks.append(_settlement_block(transfers, names, currency))

    if unconverted:
        blocks.append(
            {
                "type": "callout",
                "tone": "warning",
                "text": f"{len(unconverted)} expense(s) have no FX rate yet; "
                "balances exclude them.",
            }
        )
    elif all_settled:
        blocks.append({"type": "callout", "tone": "good", "text": "All balances are settled."})
    return validate_card({"title": title, "blocks": blocks})


def trip_summary_card(
    trip, totals, groups, names, *, currency_exponents=None, expenses=(), unconverted=()
):
    card = balance_card(
        trip,
        totals,
        groups,
        names,
        currency_exponents=currency_exponents,
        expenses=expenses,
        unconverted=unconverted,
    )
    data = card.model_dump()
    blocks = data["blocks"]
    has_categories = any(expense.get("category") for expense in expenses)
    spend = {}
    if has_categories:
        for expense in expenses:
            category = expense.get("category") or "Uncategorized"
            key = (category, expense["currency"])
            spend[key] = spend.get(key, 0) + int(expense["amount"])
        first_header, second_header = "Category", "Spend"
    else:
        for expense in expenses:
            key = (expense["spent_on"], expense["currency"])
            spend[key] = spend.get(key, 0) + int(expense["amount"])
        first_header, second_header = "Day", "Spend"
    if spend:
        ordered = sorted(spend.items(), key=lambda item: -item[1])
        rows = [
            [f"{label} ({currency})", _plain_amount(amount, currency)]
            for (label, currency), amount in ordered[:10]
        ]
        if len(ordered) > 10:
            remainder = {}
            for (_, currency), amount in ordered[10:]:
                remainder[currency] = remainder.get(currency, 0) + amount
            rows.extend(
                [f"Other ({currency})", _plain_amount(amount, currency)]
                for currency, amount in sorted(remainder.items())
            )
        blocks.append(
            {
                "type": "table",
                "columns": [
                    {"header": first_header},
                    {"header": second_header, "align": "end"},
                ],
                "rows": rows,
            }
        )
    return validate_card(data)
