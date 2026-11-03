from datetime import UTC, datetime, timedelta

import pytest
from chatcore_jobs.timing import parse_reminder_time

NOW = datetime(2026, 10, 31, tzinfo=UTC)


def test_singapore_exact_time():
    parsed = parse_reminder_time("2026-11-01T09:00:00", "Asia/Singapore", NOW)
    assert parsed.instant == datetime(2026, 11, 1, 1, tzinfo=UTC)
    assert "Asia/Singapore" in parsed.label
    assert "+08:00" in parsed.label
    assert "+00:00" in parsed.label


@pytest.mark.parametrize(
    ("local", "zone"),
    [
        ("2026-11-01T01:30:00", "America/New_York"),
        ("2026-03-08T02:30:00", "America/New_York"),
        ("2026-11-01", "Asia/Singapore"),
        ("2026-11-01T09:00:00", "Not/AZone"),
        ("2026-11-01T09:00:00", "EST"),
        ("2026-11-01T09:00:00+09:00", "Asia/Singapore"),
        ("2026-10-30T09:00:00", "Asia/Singapore"),
        ("2028-11-01T09:00:00", "Asia/Singapore"),
    ],
)
def test_invalid_reminder_times_are_rejected(local, zone):
    with pytest.raises(ValueError):
        parse_reminder_time(local, zone, NOW)


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        ("2026-11-01T01:30:00-04:00", datetime(2026, 11, 1, 5, 30, tzinfo=UTC)),
        ("2026-11-01T01:30:00-05:00", datetime(2026, 11, 1, 6, 30, tzinfo=UTC)),
    ],
)
def test_explicit_matching_offset_disambiguates_fold(local, expected):
    parsed = parse_reminder_time(local, "America/New_York", NOW)
    assert parsed.instant == expected


def test_horizon_is_inclusive_at_configured_boundary():
    with pytest.raises(ValueError):
        parse_reminder_time((NOW + timedelta(days=367)).isoformat(), "UTC", NOW)


def test_group_limits_only_reduce_global_ceilings():
    from chatcore_jobs.settings import GroupLimits, JobsSettings

    settings = JobsSettings()
    assert GroupLimits.from_values(settings, max_jobs_per_chat=50).max_jobs_per_chat == 50
    with pytest.raises(ValueError):
        GroupLimits.from_values(settings, max_jobs_per_chat=101)


def test_settings_reject_bool_as_integer_and_invalid_lease():
    from chatcore_jobs.settings import JobsSettings

    with pytest.raises(ValueError):
        JobsSettings(max_jobs_per_chat=True)
    with pytest.raises(ValueError):
        JobsSettings(lease_seconds=20)


def test_settings_accept_decimal_strings_from_app_composition():
    from chatcore_jobs.settings import JobsSettings

    assert JobsSettings(worker_concurrency="3").worker_concurrency == 3
    with pytest.raises(ValueError):
        JobsSettings(worker_concurrency=True)


def test_group_limits_reject_nonpositive_values():
    from chatcore_jobs.settings import GroupLimits, JobsSettings

    with pytest.raises(ValueError):
        GroupLimits.from_values(JobsSettings(), max_jobs_per_chat=0)
    with pytest.raises(ValueError):
        GroupLimits.from_values(JobsSettings(), minimum_interval_seconds=0)
