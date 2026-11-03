"""Strict local-to-UTC reminder parsing with explicit DST ambiguity handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class ParsedReminderTime:
    instant: datetime
    label: str


def parse_reminder_time(
    local_time: str, timezone: str, now: datetime, *, horizon_days: int = 366
) -> ParsedReminderTime:
    if not isinstance(local_time, str) or not isinstance(timezone, str):
        raise ValueError("date/time and IANA timezone are required")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if timezone != "UTC" and "/" not in timezone:
        raise ValueError("an IANA timezone identifier is required")
    try:
        zone = ZoneInfo(timezone)
        parsed = datetime.fromisoformat(local_time)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("invalid ISO date/time or IANA timezone") from exc
    if parsed.date() and ("T" not in local_time and "t" not in local_time):
        raise ValueError("date and time are required")
    if parsed.second is None:
        raise ValueError("seconds precision is required")

    if parsed.tzinfo is None:
        candidates: dict[datetime, datetime] = {}
        for fold in (0, 1):
            local = parsed.replace(tzinfo=zone, fold=fold)
            utc = local.astimezone(UTC)
            if utc.astimezone(zone).replace(tzinfo=None) == parsed:
                candidates[utc] = local
        if len(candidates) != 1:
            raise ValueError("local time is ambiguous or does not exist in this timezone")
        instant, local = next(iter(candidates.items()))
    else:
        instant = parsed.astimezone(UTC)
        local = instant.astimezone(zone)
        if local.replace(tzinfo=None) != parsed.replace(tzinfo=None):
            raise ValueError("offset does not match the named timezone")
        if local.utcoffset() != parsed.utcoffset():
            raise ValueError("offset does not match the named timezone")
    now_utc = now.astimezone(UTC)
    if instant <= now_utc:
        raise ValueError("reminder time must be in the future")
    if instant > now_utc + timedelta(days=horizon_days):
        raise ValueError("reminder time is beyond the scheduling horizon")
    offset = local.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}"
    label = (
        f"{local.replace(tzinfo=None).isoformat(sep=' ')} {timezone} "
        f"(UTC{offset}; {instant.isoformat()})"
    )
    return ParsedReminderTime(instant, label)
