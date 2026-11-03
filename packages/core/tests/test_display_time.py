from datetime import UTC, datetime

import pytest
from chatcore.display_time import format_instant, validate_zone


def test_validates_installed_iana_zone_and_utc() -> None:
    assert validate_zone("Asia/Singapore") == "Asia/Singapore"
    assert validate_zone("UTC") == "UTC"


@pytest.mark.parametrize("value", ["PST", "EST", "CET", "../UTC", "/etc/passwd", "Not/AZone", ""])
def test_rejects_abbreviations_paths_and_unknown_zones(value: str) -> None:
    with pytest.raises(ValueError):
        validate_zone(value)


def test_formats_named_zone_with_numeric_offset() -> None:
    at = datetime(2026, 11, 1, 1, tzinfo=UTC)
    assert format_instant(at, "Asia/Singapore") == (
        "2026-11-01 09:00:00 Asia/Singapore (UTC+08:00)"
    )


def test_formats_both_sides_of_daylight_saving_fold() -> None:
    first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    second = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert "UTC-04:00" in format_instant(first, "America/New_York")
    assert "UTC-05:00" in format_instant(second, "America/New_York")


def test_rejects_naive_instants() -> None:
    with pytest.raises(ValueError, match="aware"):
        format_instant(datetime(2026, 1, 1), "UTC")
