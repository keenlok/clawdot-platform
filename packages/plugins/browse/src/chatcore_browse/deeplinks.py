"""Strict validation and construction of user-opened search links."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from unicodedata import category
from urllib.parse import urlencode

from chatcore.tools.registry import ToolContext, ToolResult

from chatcore_browse.templates import TEMPLATES
from chatcore_browse.urls import normalize_url

_IATA = re.compile(r"^[A-Za-z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}$")
_FILTER_LABEL = {
    "economy": "Small car",
    "compact": "Small car",
    "midsize": "Medium car",
    "suv": "SUVs",
    "van": "People carriers",
}
_DISCLAIMER = (
    "This is only a search link. It does not confirm availability or price; "
    "the site shows live results."
)


def _clean_text(value: object, label: str, max_len: int = 120) -> str:
    if (
        not isinstance(value, str)
        or len(value) > max_len
        or any(category(ch) == "Cc" for ch in value)
    ):
        raise ValueError(f"{label} is invalid.")
    return value.strip()


def _parse_date(value: object, label: str) -> date:
    raw = _clean_text(value, label, 10)
    if not _DATE.fullmatch(raw):
        raise ValueError(f"{label} must use YYYY-MM-DD.")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{label} is not a real date.") from exc


def _parse_time(value: object, label: str) -> time:
    raw = _clean_text(value, label, 5)
    if not _TIME.fullmatch(raw):
        raise ValueError(f"{label} must use HH:MM.")
    try:
        parsed = time.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid.") from exc
    if parsed.minute not in (0, 30):
        raise ValueError(f"{label} minutes must be 00 or 30.")
    return parsed


def _format_link(template, values: dict[str, object]) -> str:
    query = [
        (param, str(formatter(values[field])))
        for field, (param, formatter) in template.field_params.items()
    ]
    return f"{template.scheme}://{template.host}{template.path}" + (
        f"?{urlencode(query)}" if query else ""
    )


def build_search_link(
    ctx: ToolContext,
    *,
    site: str,
    kind: str,
    location: str,
    pickup_date: str,
    return_date: str,
    pickup_time: str = "10:00",
    return_time: str = "10:00",
    car_class: str | None = None,
    driver_age: int = 30,
    clock=lambda: datetime.now(UTC),
    **extra,
) -> ToolResult:
    try:
        if extra:
            raise ValueError("Unknown argument.")
        if not isinstance(site, str) or site not in TEMPLATES:
            raise ValueError("Choose a registered rental site.")
        template = TEMPLATES[site]
        if kind not in template.kinds:
            raise ValueError("That search type is not supported.")
        location_value = _clean_text(location, "Location")
        if site == "rentalcars":
            if not _IATA.fullmatch(location_value):
                raise ValueError("Location must be a three-letter airport code.")
            location_value = location_value.upper()
        elif not location_value or not all(
            character.isalpha() or character.isdigit() or character == " " or character in "-.,'()/"
            for character in location_value
        ):
            raise ValueError("Location is invalid.")
        pickup_day, return_day = (
            _parse_date(pickup_date, "Pickup date"),
            _parse_date(return_date, "Return date"),
        )
        pickup_clock = _parse_time(pickup_time, "Pickup time")
        return_clock = _parse_time(return_time, "Return time")
        now = clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now = now.astimezone(UTC)
        pickup = datetime.combine(pickup_day, pickup_clock)
        returned = datetime.combine(return_day, return_clock)
        if pickup_day < now.date() - timedelta(days=1):
            raise ValueError("Pickup must not be in the past.")
        if pickup_day > now.date() + timedelta(days=365):
            raise ValueError("Pickup must be within 365 days.")
        if returned <= pickup:
            raise ValueError("Return must be after pickup.")
        if returned - pickup > timedelta(days=90):
            raise ValueError("Rental must be 90 days or shorter.")
        if type(driver_age) is not int or not 21 <= driver_age <= 80:
            raise ValueError("Driver age must be from 21 to 80.")
        if car_class is not None and (
            not isinstance(car_class, str) or car_class not in _FILTER_LABEL
        ):
            raise ValueError("Choose a supported car class.")
        values: dict[str, object] = {
            "location": location_value,
            "pickup_day": pickup_day,
            "pickup_month": pickup_day,
            "pickup_year": pickup_day,
            "pickup_hour": pickup_clock,
            "pickup_minute": pickup_clock,
            "return_day": return_day,
            "return_month": return_day,
            "return_year": return_day,
            "return_hour": return_clock,
            "return_minute": return_clock,
            "driver_age": driver_age,
            "fts_type": "A",
        }
        url = _format_link(template, values)
        if template.prefill:
            lines = [
                url,
                f"Pickup: {location_value}, {pickup_day.isoformat()} at {pickup_clock:%H:%M}",
                f"Return: {return_day.isoformat()} at {return_clock:%H:%M}",
                f"Driver age: {driver_age}",
            ]
            if car_class:
                lines.append(
                    f"Car class is not prefilled; use the '{_FILTER_LABEL[car_class]}' "
                    "filter on the results page."
                )
            lines.append(_DISCLAIMER)
        else:
            lines = [
                url,
                f'Pickup shop: "{location_value}"',
                f"Pickup date and time: {pickup_day.isoformat()} at {pickup_clock:%H:%M}",
                f"Return date and time: {return_day.isoformat()} at {return_clock:%H:%M}",
                "This page could not be prefilled; enter the values above on the reservation page.",
            ]
        ctx.turn.seen_urls.add(normalize_url(url))
        return ToolResult(content="\n".join(lines))
    except (ValueError, TypeError, OverflowError):
        return ToolResult.error(
            "I couldn't build that search link. Check the site, location, and dates."
        )
