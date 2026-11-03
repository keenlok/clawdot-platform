import pytest
from chatcore_research.settings import ResearchSettings
from pydantic import ValidationError


def test_research_defaults_and_long_timeout_constraint():
    settings = ResearchSettings()

    assert settings.inline_max_searches == 6
    assert settings.long_timeout_s > settings.inline_timeout_s
    assert settings.lease_seconds == settings.long_timeout_s + 60


def test_research_bounds_are_validated():
    with pytest.raises(ValidationError):
        ResearchSettings(tool_timeout_s=9)
