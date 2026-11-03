"""Turn requested split rules into exact minor-unit shares."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation


def split_amount(
    amount: int,
    participants: list[str] | tuple[str, ...],
    payer: str,
    split_type: str = "equal",
    values: Mapping[str, int | float | str | Decimal] | None = None,
    *,
    currency: str = "SGD",
) -> dict[str, int]:
    """Allocate every minor unit, assigning any rounding remainder deterministically."""
    people = list(dict.fromkeys(participants))
    if amount <= 0 or not people:
        raise ValueError("Amount and participants must be positive/non-empty.")
    if values is not None and set(values) != set(people):
        raise ValueError("Split values must be provided for exactly the participants.")
    recipient = payer if payer in people else people[0]
    if split_type == "equal":
        result = {p: amount // len(people) for p in people}
        result[recipient] += amount - sum(result.values())
        return result
    if values is None:
        raise ValueError(f"{split_type} split requires values for each participant.")
    if split_type == "exact":
        result = {p: _minor(values[p]) for p in people}
        if any(v < 0 for v in result.values()) or sum(result.values()) != amount:
            raise ValueError("Exact split amounts must be non-negative and sum to the total.")
        return result
    if split_type not in {"percentage", "shares"}:
        raise ValueError("Split type must be equal, exact, percentage, or shares.")
    try:
        weights = {p: Decimal(str(values[p])) for p in people}
    except InvalidOperation as exc:
        raise ValueError("Split values must be valid numbers.") from exc
    if any(not w.is_finite() or w < 0 for w in weights.values()):
        raise ValueError("Split values must be finite and non-negative.")
    total_weight = sum(weights.values(), Decimal(0))
    if split_type == "percentage" and total_weight != Decimal(100):
        raise ValueError("Percentages must sum to 100.")
    if total_weight <= 0:
        raise ValueError("Split weights must sum to more than zero.")
    result = {p: int(Decimal(amount) * weights[p] / total_weight) for p in people}
    result[recipient] += amount - sum(result.values())
    return result


def _minor(value: int | float | str | Decimal) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError("Exact shares must be integer minor units.")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Exact shares must be integer minor units.") from exc
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise ValueError("Exact shares must be integer minor units.")
    return int(parsed)
