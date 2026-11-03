"""Pure itinerary schema, grouping, validation, clashes and currency totals."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

HEADER = ["Day", "Time", "Place", "Type", "Cost", "Booking ref", "Status", "Notes"]
VERSION = ["chatcore-itinerary", "1"]
KEYS = ("day", "time", "place", "type", "cost", "booking_ref", "status", "notes")
STATUSES = {"Proposed", "Confirmed", "Done", "Dropped"}
TYPES = {"Stay", "Food", "Sight", "Transit", "Other", ""}


def tracker_rows(values):
    if len(values) < 2 or values[0][:2] != VERSION or values[1][:8] != HEADER:
        raise ValueError("Unrecognized itinerary header/version; expected chatcore-itinerary v1.")
    if any(str(cell).strip() for cell in values[0][2:] + values[1][8:]):
        raise ValueError("Unrecognized itinerary header/version; extra header cells are present.")
    # Preserve blank rows so every reported row number matches its position in Sheets.
    return values[2:]


def normalize_groups(groups, settings, *, suggest=False, sources=frozenset()):
    if not isinstance(groups, list) or not 0 < len(groups) <= settings.max_tabs:
        raise ValueError("Provide a bounded, nonempty list of named day/city tabs.")
    result, names, count = [], set(), 0
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"title", "rows"}:
            raise ValueError("Each tab requires exactly title and rows.")
        title = group["title"]
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 100
            or re.search(r"[\[\]:*?/\\\x00-\x1f]", title)
            or title.casefold() in names
        ):
            raise ValueError("Provide unique valid tab names (up to 100 characters).")
        names.add(title.casefold())
        raw_rows = group["rows"]
        if not isinstance(raw_rows, list) or not raw_rows:
            raise ValueError("Each proposed tab must have at least one row.")
        normalized = []
        for raw in raw_rows:
            if not isinstance(raw, dict) or set(raw) - set(KEYS) - {"source_url"}:
                raise ValueError("Unknown itinerary row fields.")
            if any(not isinstance(value, str) for value in raw.values()):
                raise ValueError("Itinerary fields must be text; leave unknown values blank.")
            row = [raw.get(key, "") for key in KEYS]
            row[6] = raw.get("status", "Proposed")
            if row[6] not in STATUSES or row[3] not in TYPES:
                raise ValueError("Unrecognized itinerary Type or Status.")
            if suggest:
                source = raw.get("source_url", "")
                if source not in sources or urlsplit(source).scheme not in {"http", "https"}:
                    raise ValueError(
                        "Every suggestion needs a posted or search/research-returned source URL."
                    )
                row[6] = "Proposed"
                row[7] += (
                    ("\n" if row[7] else "")
                    + source
                    + "\nHours/prices unverified; check before booking."
                )
            if any(len(value) > settings.max_cell_chars for value in row):
                raise ValueError("An itinerary field exceeds the cell limit.")
            normalized.append(row)
            count += 1
        result.append({"title": title, "rows": normalized})
    if count > (settings.suggestion_rows_per_call if suggest else settings.max_rows):
        raise ValueError("This itinerary batch exceeds the row limit.")
    if len(json.dumps(result, ensure_ascii=False)) > settings.max_chars:
        raise ValueError("This itinerary batch exceeds the character limit.")
    return result


def day_number(value):
    try:
        return "date", date.fromisoformat(value).toordinal()
    except ValueError:
        match = re.fullmatch(r"(?:Day\s+)?([1-9][0-9]{0,3})", value, re.I)
        return ("relative", int(match[1])) if match else None


def day_label(kind, number):
    return date.fromordinal(number).isoformat() if kind == "date" else f"Day {number}"


def time_range(value):
    match = re.fullmatch(r"([0-2][0-9]):([0-5][0-9])\s*[-–]\s*([0-2][0-9]):([0-5][0-9])", value)
    if not match:
        return None
    h1, m1, h2, m2 = map(int, match.groups())
    if h1 > 23 or h2 > 23:
        return None
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if end <= start:
        end += 1440
    return start, end


def cost(value):
    match = re.fullmatch(
        r"(?:([A-Z]{3})\s+([0-9]+(?:\.[0-9]+)?)|([0-9]+(?:\.[0-9]+)?)\s+([A-Z]{3}))", value
    )
    if not match:
        return None
    first, amount, amount2, last = match.groups()
    if len(amount or amount2) > 24:
        return None
    return first or last, Decimal(amount or amount2)


def summarize(tabs, *, partial=False):
    totals_partial = partial
    warnings, clashes, gaps = [], [], []
    totals, days, periods = defaultdict(Decimal), defaultdict(list), defaultdict(set)
    intervals = []
    for title, rows in tabs:
        for index, raw in enumerate(rows, 3):
            if not any(str(value).strip() for value in raw):
                continue
            label = f"{title}, row {index}"
            if any(str(value).strip() for value in raw[len(HEADER) :]):
                warnings.append(label + ": expected eight cells (trailing blanks are allowed).")
                totals_partial = True
            row = [str(cell) for cell in raw[:8]] + [""] * max(0, 8 - len(raw))
            day, clock, place, kind, price, _, status, _ = row
            if status == "Dropped":
                continue
            if status not in STATUSES:
                warnings.append(label + ": unrecognized or missing Status.")
                totals_partial = True
            if kind not in TYPES:
                warnings.append(label + ": unrecognized Type.")
            parsed_day, parsed_time, parsed_cost = day_number(day), time_range(clock), cost(price)
            if not parsed_day:
                warnings.append(label + ": missing or unparseable Day.")
            else:
                days[parsed_day].append(
                    {
                        "tab": title,
                        "row": index,
                        "time": clock,
                        "place": place,
                        "type": kind,
                        "cost": price,
                        "status": status,
                    }
                )
            if not parsed_time:
                warnings.append(label + ": missing or unparseable time range (HH:MM-HH:MM).")
            if not parsed_cost:
                warnings.append(label + ": missing or unparseable cost/currency.")
                totals_partial = True
            else:
                currency, amount = parsed_cost
                totals[currency] += amount
            if not place:
                warnings.append(label + ": missing Place.")
            if parsed_day and parsed_time:
                basis, number = parsed_day
                start, end = parsed_time
                intervals.append((basis, number * 1440 + start, number * 1440 + end, label, kind))
                if kind != "Stay":
                    periods[parsed_day].add("morning" if start < 720 else "afternoon")
                if kind != "Stay" and start < 720 and end > 720:
                    periods[parsed_day].add("afternoon")
                if kind != "Stay" and end > 1440:
                    periods[(basis, number + 1)].add("morning")
    for index, (basis, start, end, label, kind) in enumerate(intervals):
        for other_basis, other_start, other_end, other_label, other_kind in intervals[index + 1 :]:
            if (kind == "Stay") != (other_kind == "Stay"):
                continue
            if basis == other_basis and start < other_end and other_start < end:
                clashes.append(label + " overlaps " + other_label)
    for basis in {value[0] for value in days}:
        numbers = [value[1] for value in days if value[0] == basis]
        if max(numbers) - min(numbers) > 365:
            warnings.append("Trip date span exceeds one year; gap checking is partial.")
            partial = True
        for number in range(min(numbers), min(max(numbers), min(numbers) + 365) + 1):
            key = (basis, number)
            if key not in days and key not in periods:
                gaps.append(day_label(*key) + ": no planned items.")
            elif key in days or periods.get(key):
                for period in ("morning", "afternoon"):
                    if period not in periods.get(key, set()):
                        gaps.append(day_label(*key) + f": no timed {period} items.")
    return {
        "partial": partial,
        "totals_partial": totals_partial,
        "days": {day_label(*key): values for key, values in sorted(days.items())},
        "planned_totals": {currency: str(amount) for currency, amount in totals.items()},
        "clashes": clashes,
        "gaps": gaps,
        "warnings": warnings,
    }
