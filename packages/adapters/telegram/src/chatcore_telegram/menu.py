"""Telegram command-menu size policy.

Telegram accepts at most 100 commands per menu scope; a longer list makes ``setMyCommands`` fail
and leaves the menu empty. Commands are only trimmed from the menu once it nears that limit
(skill-layer decision U5): below ``MENU_TRIM_AT`` everything is listed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

MENU_LIMIT = 100
MENU_TRIM_AT = 90

log = logging.getLogger(__name__)


def fit_menu[T](items: Sequence[T], *, scope: str) -> list[T]:
    """Return at most ``MENU_LIMIT`` items, warning once the menu is due for trimming."""
    count = len(items)
    if count > MENU_LIMIT:
        log.error(
            "telegram menu over limit scope=%s commands=%d limit=%d; extra commands dropped, "
            "trim the menu",
            scope,
            count,
            MENU_LIMIT,
        )
    elif count >= MENU_TRIM_AT:
        log.warning(
            "telegram menu near limit scope=%s commands=%d limit=%d; time to trim the menu",
            scope,
            count,
            MENU_LIMIT,
        )
    return list(items[:MENU_LIMIT])
