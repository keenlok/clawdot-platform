"""Poll text, date preset, local time and settings boundaries."""

from datetime import UTC, datetime, timedelta

import pytest
from chatcore.polls import PollLimits
from chatcore_polls.parsing import parse_poll
from chatcore_polls.service import build_draft
from chatcore_polls.settings import PollsSettings
from chatcore_polls.timing import parse_close_time
from pydantic import ValidationError

NOW = datetime(2026, 10, 31, tzinfo=UTC)
LIMITS = PollLimits(12, 300, 100)
SETTINGS = PollsSettings()


def draft(text):
    return build_draft(
        **parse_poll(text, max_options=12), limits=LIMITS, settings=SETTINGS, now=NOW
    )


@pytest.mark.parametrize("question", ["Dinner?", '"Dinner?"', "“Dinner?”"])
def test_plain_and_quoted_question(question):
    result = draft(f"{question} | Ramen | Tacos")
    assert result.question == "Dinner?" and result.options == ("Ramen", "Tacos")
    assert result.multiple is False and result.anonymous is False
    assert result.close_at is None


@pytest.mark.parametrize(
    "flags,multiple,anonymous",
    [
        ("--multi", True, False),
        ("--single", False, False),
        ("--anon", False, True),
        ("--anon --multi", True, True),
        ("--multi --anon", True, True),
    ],
)
def test_flags(flags, multiple, anonymous):
    result = draft(flags + " Dinner? | A | B")
    assert result.multiple == multiple and result.anonymous == anonymous


@pytest.mark.parametrize(
    "text",
    [
        "--multi --multi Q | A | B",
        "--anon --anon Q | A | B",
        "--bad Q | A | B",
        "--multi --single Q | A | B",
        "--close",
        "--close time zone --close time zone Q | A | B",
        "",
        "Q",
        "dates bad..bad",
        "dates 2026-11-10..2026-11-01",
    ],
)
def test_malformed_commands(text):
    with pytest.raises(ValueError):
        draft(text)


@pytest.mark.parametrize(
    "text",
    [
        "Q | A",
        "Q | A | a",
        "Q | A | ",
        "Q | " + " | ".join(map(str, range(13))),
        "Q" * 301 + " | A | B",
        "Q | " + "A" * 101 + " | B",
        " | A | B",
    ],
)
def test_invalid_text(text):
    with pytest.raises(ValueError):
        draft(text)


def test_close_and_exact_summary_label():
    result = draft("--close 2026-11-05T18:00:00 Asia/Singapore Dinner? | A | B")
    assert result.close_at == datetime(2026, 11, 5, 10, tzinfo=UTC)
    assert result.close_label == (
        "2026-11-05 18:00:00 Asia/Singapore (UTC+08:00; 2026-11-05T10:00:00+00:00)"
    )


@pytest.mark.parametrize(
    "local,zone",
    [
        ("2026-11-05T18:00", "Asia/Singapore"),
        ("2026-11-05T18:00:00", "bad/zone"),
        ("2026-11-05T18:00:00", "SGT"),
        ("2026-10-30T18:00:00", "UTC"),
        ("2027-03-01T18:00:00", "UTC"),
        ("2026-10-31T00:00:30", "UTC"),
        ("2026-11-01T01:30:00", "America/New_York"),
        ("2027-03-14T02:30:00", "America/New_York"),
        ("2026-11-05T18:00:00+00:00", "Asia/Singapore"),
        ("2026-11-05", "UTC"),
    ],
)
def test_bad_close(local, zone):
    with pytest.raises(ValueError):
        parse_close_time(local, zone, NOW, horizon_days=90, min_lead_seconds=60)


def test_matching_offset_disambiguates_dst_and_bounds_are_inclusive():
    assert parse_close_time("2026-11-01T01:30:00-04:00", "America/New_York", NOW).instant.hour == 5
    for seconds in (60, 30 * 86400):
        instant = NOW + timedelta(seconds=seconds)
        assert parse_close_time(instant.isoformat(), "UTC", NOW).instant == instant


def test_dates_ten_days_multi_default_and_single_override():
    result = draft("dates 2026-11-01..2026-11-10")
    assert len(result.options) == 10 and result.options[0] == "Sun 1 Nov"
    assert result.question == "Which dates work?" and result.multiple
    assert not draft("--single dates 2026-11-01..2026-11-02 Dinner?").multiple
    assert draft("--multi dates 2026-11-01..2026-11-02").multiple
    result = draft("dates 2026-12-31..2027-01-01")
    assert result.options == ("Thu 31 Dec 2026", "Fri 1 Jan 2027")


def test_dates_max_in_error_and_platform_limit():
    with pytest.raises(ValueError, match=r"13.*12|12.*13"):
        draft("dates 2026-11-01..2026-11-13")
    with pytest.raises(ValueError, match="3"):
        build_draft(
            "Q", ["A", "B", "C", "D"], limits=PollLimits(3, 300, 100), settings=SETTINGS, now=NOW
        )


@pytest.mark.parametrize(
    "name,lo,hi",
    [
        ("max_options", 2, 12),
        ("max_open_per_chat", 1, 200),
        ("max_open_per_user", 1, 50),
        ("close_horizon_days", 1, 90),
        ("min_close_lead_seconds", 5, 3600),
        ("scan_seconds", 1, 300),
        ("lease_seconds", 10, 600),
        ("max_close_failures", 1, 10),
        ("retention_days", 1, 3650),
    ],
)
def test_settings_bounds_and_strict_types(name, lo, hi):
    assert getattr(PollsSettings(**{name: str(lo)}), name) == lo
    assert getattr(PollsSettings(**{name: hi}), name) == hi
    for value in (True, 1.5, lo - 1, hi + 1):
        with pytest.raises(ValidationError):
            PollsSettings(**{name: value})


@pytest.mark.parametrize("text", ["dates 20261101..20261102", "dates 2026-W45-1..2026-W45-2"])
def test_dates_require_hyphenated_calendar_dates(text):
    with pytest.raises(ValueError):
        draft(text)


@pytest.mark.parametrize(
    "question",
    ["dates night?", "dates", "dates 20261101..20261102", "dates 2026-W45-1..2026-W45-2"],
)
def test_dates_without_calendar_range_shape_is_an_ordinary_question(question):
    result = draft(f"{question} | Fri | Sat")
    assert result.question == question
    assert result.options == ("Fri", "Sat")
    assert result.multiple is False


@pytest.mark.parametrize(
    "date_range,expected",
    [
        ("2026-02-30..2026-03-01", "usage"),
        ("2026-11-10..2026-11-01", "usage"),
        ("2026-11-01..2026-11-13", "That range has 13 dates; the maximum is 12 options."),
    ],
)
@pytest.mark.parametrize("suffix", ["", " | Fri | Sat"])
def test_well_shaped_invalid_date_ranges_keep_their_errors(date_range, expected, suffix):
    from chatcore_polls.parsing import USAGE

    with pytest.raises(ValueError) as exc:
        draft(f"dates {date_range}{suffix}")
    assert str(exc.value) == (USAGE if expected == "usage" else expected)
