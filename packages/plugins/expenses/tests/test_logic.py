import pytest
from chatcore_expenses.balances import simplify_balances
from chatcore_expenses.currency import parse_amount, resolve_currency
from chatcore_expenses.split import split_amount


def test_equal_split_remainder_goes_to_payer():
    assert split_amount(10, ["a", "b", "c"], "a", "equal") == {
        "a": 4,
        "b": 3,
        "c": 3,
    }


def test_zero_decimal_currency_equal_split():
    assert split_amount(1000, ["a", "b", "c"], "b", "equal", currency="JPY") == {
        "a": 333,
        "b": 334,
        "c": 333,
    }


def test_exact_percent_and_shares():
    assert split_amount(100, ["a", "b"], "a", "exact", {"a": 40, "b": 60}) == {
        "a": 40,
        "b": 60,
    }
    assert split_amount(100, ["a", "b"], "a", "percentage", {"a": 25, "b": 75}) == {
        "a": 25,
        "b": 75,
    }
    assert split_amount(100, ["a", "b"], "a", "shares", {"a": 1, "b": 3}) == {
        "a": 25,
        "b": 75,
    }


def test_percentage_must_sum_to_100():
    with pytest.raises(ValueError, match="100"):
        split_amount(100, ["a", "b"], "a", "percentage", {"a": 40, "b": 40})


def test_balances_simplify_to_at_most_n_minus_one_transfers():
    result = simplify_balances({"a": -5, "b": -5, "c": 6, "d": 4})
    assert len(result) <= 3
    assert sum(amount for _, _, amount in result) == 10


def test_amount_parser_uses_decimal_and_rejects_currency_symbols():
    assert parse_amount("1,234.50", "SGD") == 123450
    with pytest.raises(ValueError, match="separate currency"):
        parse_amount("¥3000", "JPY")


def test_currency_resolution_asks_on_multicurrency_trip():
    with pytest.raises(ValueError, match="JPY.*KRW"):
        resolve_currency(None, ["JPY", "KRW"], trip_active=True)


def test_convert_balance_rounds_half_even_after_aggregation():
    from chatcore_expenses.balances import convert_balances

    expenses = [
        {
            "currency": "USD",
            "amount": 1,
            "paid_by": "a",
            "fx_rate_to_settle": "0.5",
            "shares": [{"user_key": "b", "amount": 1}],
        },
        {
            "currency": "USD",
            "amount": 1,
            "paid_by": "a",
            "fx_rate_to_settle": "0.5",
            "shares": [{"user_key": "b", "amount": 1}],
        },
    ]
    values, unconverted = convert_balances(expenses, [], "SGD")
    assert values == {"a": 1, "b": -1}
    assert unconverted == []


def test_per_currency_net_balances_keep_currency_separate():
    from chatcore_expenses.balances import net_balances

    expenses = [
        {
            "currency": "JPY",
            "amount": 1000,
            "paid_by": "a",
            "shares": [{"user_key": "b", "amount": 1000}],
        },
        {
            "currency": "KRW",
            "amount": 2000,
            "paid_by": "b",
            "shares": [{"user_key": "a", "amount": 2000}],
        },
    ]
    assert net_balances(expenses) == {
        "a": {"JPY": 1000, "KRW": -2000},
        "b": {"JPY": -1000, "KRW": 2000},
    }


def test_country_currency_map_matches_name_and_iso_case_insensitively():
    from chatcore_expenses.currency import currency_for_country

    assert currency_for_country("japan") == "JPY"
    assert currency_for_country("JP") == "JPY"
    assert currency_for_country("FR") == "EUR"


def test_remainder_goes_to_first_participant_if_payer_is_absent():
    assert split_amount(10, ["b", "a", "c"], "payer", "equal") == {
        "b": 4,
        "a": 3,
        "c": 3,
    }


def test_unknown_currency_is_rejected():
    with pytest.raises(ValueError, match="Unknown ISO"):
        parse_amount("5.00", "ZZZ")


def test_amount_parser_rejects_malformed_grouping():
    with pytest.raises(ValueError, match="valid decimal"):
        parse_amount("12,34", "SGD")


def test_exact_split_requires_exact_sum():
    with pytest.raises(ValueError, match="sum to the total"):
        split_amount(100, ["a", "b"], "a", "exact", {"a": 40, "b": 50})


def test_converted_balances_remain_zero_sum_after_final_rounding():
    from chatcore_expenses.balances import convert_balances

    expense = {
        "currency": "USD",
        "amount": 2,
        "paid_by": "payer",
        "fx_rate_to_settle": "0.5",
        "shares": [{"user_key": "b", "amount": 1}, {"user_key": "c", "amount": 1}],
    }
    balances, _ = convert_balances([expense], [], "SGD")
    assert sum(balances.values()) == 0
    assert simplify_balances(balances)
