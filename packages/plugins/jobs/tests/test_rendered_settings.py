import pytest
from chatcore_jobs.settings import JobsSettings


def test_rendered_settings_have_separate_cost_bounds():
    settings = JobsSettings()
    assert settings.minimum_rendered_watch_interval_seconds == 3600
    assert settings.max_rendered_watches_global == 20
    assert settings.rendered_concurrency == 1
    assert settings.rendered_fetch_timeout_seconds == 35
    with pytest.raises(ValueError):
        JobsSettings(rendered_concurrency=5)
    with pytest.raises(ValueError):
        JobsSettings(minimum_rendered_watch_interval_seconds=59)


def test_plain_watch_settings_remain_valid_with_rendering_disabled_or_empty_window():
    short_plain_window = JobsSettings(maximum_watch_interval_seconds=600)
    assert short_plain_window.maximum_watch_interval_seconds == 600
    disabled_rendering = JobsSettings(max_rendered_watches_global=0)
    assert disabled_rendering.max_rendered_watches_global == 0


def test_effective_rendered_timeout_is_bounded_by_execution_deadline():
    settings = JobsSettings(
        rendered_fetch_timeout_seconds=35,
        execution_deadline_seconds=20,
        send_deadline_seconds=15,
    )
    assert settings.rendered_fetch_timeout_seconds == 35
    assert settings.effective_rendered_fetch_timeout_seconds == min(
        settings.rendered_fetch_timeout_seconds, settings.execution_deadline_seconds
    )
