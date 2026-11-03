"""Verified search-link templates. Templates are data, never executable URL builders."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class LinkTemplate:
    site_id: str
    label: str
    kinds: frozenset[str]
    scheme: str
    host: str
    path: str
    prefill: bool
    field_params: dict[str, tuple[str, Callable[[object], object]]]
    verified_on: date
    source_note: str


def _date_parts(part: str):
    return lambda value: getattr(value, part)


TEMPLATES = {
    "rentalcars": LinkTemplate(
        "rentalcars",
        "Rentalcars.com",
        frozenset({"rental_car"}),
        "https",
        "www.rentalcars.com",
        "/search-results",
        True,
        {
            "location": ("locationIata", str),
            "pickup_day": ("puDay", _date_parts("day")),
            "pickup_month": ("puMonth", _date_parts("month")),
            "pickup_year": ("puYear", _date_parts("year")),
            "pickup_hour": ("puHour", lambda value: value.hour),
            "pickup_minute": ("puMinute", lambda value: value.minute),
            "return_day": ("doDay", _date_parts("day")),
            "return_month": ("doMonth", _date_parts("month")),
            "return_year": ("doYear", _date_parts("year")),
            "return_hour": ("doHour", lambda value: value.hour),
            "return_minute": ("doMinute", lambda value: value.minute),
            "driver_age": ("driversAge", int),
            "fts_type": ("ftsType", lambda value: "A"),
        },
        date(2026, 10, 31),
        "Live page verified with FUK dates and driver age; car class is not prefilled.",
    ),
    "toyota_rent_a_car": LinkTemplate(
        "toyota_rent_a_car",
        "Toyota Rent a Car",
        frozenset({"rental_car"}),
        "https",
        "rent.toyota.co.jp",
        "/eng/reservation/index01.aspx",
        False,
        {},
        date(2026, 10, 31),
        "English reservation uses POST and ViewState; no query-string prefill exists.",
    ),
}
