import logging

from chatcore_telegram.menu import MENU_LIMIT, MENU_TRIM_AT, fit_menu


def test_small_menu_is_unchanged_and_silent(caplog):
    items = [(f"c{i}", "d") for i in range(MENU_TRIM_AT - 1)]
    with caplog.at_level(logging.WARNING):
        assert fit_menu(items, scope="private") == items
    assert not caplog.records


def test_menu_near_limit_warns_to_trim(caplog):
    items = [(f"c{i}", "d") for i in range(MENU_TRIM_AT)]
    with caplog.at_level(logging.WARNING):
        assert fit_menu(items, scope="group") == items
    assert "time to trim the menu" in caplog.text


def test_menu_over_limit_is_cut_to_telegram_maximum(caplog):
    items = [(f"c{i}", "d") for i in range(MENU_LIMIT + 5)]
    with caplog.at_level(logging.WARNING):
        assert fit_menu(items, scope="private") == items[:MENU_LIMIT]
    assert caplog.records[0].levelno == logging.ERROR
