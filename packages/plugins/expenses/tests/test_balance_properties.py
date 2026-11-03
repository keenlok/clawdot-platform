from chatcore_expenses.balances import net_balances, simplify_balances
from chatcore_expenses.split import split_amount
from hypothesis import assume, given
from hypothesis import strategies as st


@given(st.lists(st.integers(min_value=-1000, max_value=1000), min_size=4, max_size=18))
def test_min_cash_flow_arbitrary_zero_sum_balances_with_multiple_sides(values):
    balances = {f"u{i}": value for i, value in enumerate(values)}
    balances["balancing"] = -sum(values)
    nonzero = [value for value in balances.values() if value]
    assume(sum(value < 0 for value in nonzero) >= 2)
    assume(sum(value > 0 for value in nonzero) >= 2)
    assert sum(balances.values()) == 0

    transfers = simplify_balances(balances)
    assert len(transfers) <= len(nonzero) - 1
    after = dict(balances)
    for debtor, creditor, amount in transfers:
        after[debtor] += amount
        after[creditor] -= amount
    assert all(amount == 0 for amount in after.values())


@given(
    amount=st.integers(min_value=1, max_value=100_000),
    count=st.integers(min_value=1, max_value=8),
    kind=st.sampled_from(["equal", "exact", "percentage", "shares"]),
    payer_outside=st.booleans(),
)
def test_random_expense_splits_net_to_zero_per_currency(amount, count, kind, payer_outside):
    participants = [f"user-{index}" for index in range(count)]
    payer = "payer-outside" if payer_outside else participants[-1]
    values = None
    if kind == "exact":
        values = {user: amount // count for user in participants}
        values[participants[0]] += amount - sum(values.values())
    elif kind == "percentage":
        values = {user: 100 // count for user in participants}
        values[participants[0]] += 100 - sum(values.values())
    elif kind == "shares":
        values = {user: index + 1 for index, user in enumerate(participants)}

    shares = split_amount(amount, participants, payer, kind, values)
    balances = net_balances(
        [
            {
                "paid_by": payer,
                "currency": "JPY",
                "amount": amount,
                "shares": [{"user_key": user, "amount": share} for user, share in shares.items()],
            }
        ]
    )
    assert sum(value.get("JPY", 0) for value in balances.values()) == 0
