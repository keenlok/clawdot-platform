"""Validation and rendering for user-facing instants; storage and scheduling remain UTC."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

_ZONES = available_timezones() | {"UTC"}


def validate_zone(value: str) -> str:
    """Return an installed IANA zone key, rejecting aliases not shipped by this runtime."""
    if not isinstance(value, str) or value not in _ZONES or (value != "UTC" and "/" not in value):
        raise ValueError("Use an installed IANA time zone such as Europe/London or UTC.")
    try:
        ZoneInfo(value)
    except (KeyError, ValueError):
        raise ValueError("Use an installed IANA time zone such as Europe/London or UTC.") from None
    return value


def format_instant(instant: datetime, zone: str) -> str:
    """Render an aware instant with its IANA zone and numeric offset, including DST folds."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must be timezone-aware")
    name = validate_zone(zone)
    local = instant.astimezone(ZoneInfo(name))
    offset = local.utcoffset()
    if offset is None:
        raise ValueError("zone has no UTC offset")
    seconds = int(offset.total_seconds())
    sign = "+" if seconds >= 0 else "-"
    minutes = abs(seconds) // 60
    return f"{local:%Y-%m-%d %H:%M:%S} {name} (UTC{sign}{minutes // 60:02d}:{minutes % 60:02d})"
