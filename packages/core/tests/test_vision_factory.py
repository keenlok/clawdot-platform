import pytest
from chatcore.llm.factory import VisionSettings, build_vision_provider
from chatcore.llm.openai_compat import OpenAICompatProvider


def test_vision_provider_unset_returns_none():
    assert build_vision_provider(VisionSettings()) is None


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"VISION_PROVIDER": "openai_compat", "VISION_MODEL": "model"}, "VISION_API_KEY"),
        ({"VISION_PROVIDER": "openai_compat", "VISION_API_KEY": "fake-key"}, "VISION_MODEL"),
    ],
)
def test_vision_provider_requires_key_and_model_without_revealing_values(values, field):
    with pytest.raises(ValueError, match=field) as error:
        build_vision_provider(VisionSettings(**values))
    assert "fake-key" not in str(error.value)


def test_opencode_vision_provider_uses_openai_compat_path():
    provider = build_vision_provider(
        VisionSettings(
            VISION_PROVIDER="openai_compat",
            VISION_BASE_URL="https://opencode.ai/zen/go/v1",
            VISION_MODEL="mimo-v2.6-flash",
            VISION_API_KEY="fake-key",
        )
    )
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.model == "mimo-v2.6-flash"
    assert provider._opencode


def test_describe_and_receipt_settings_defaults_and_bounds():
    settings = VisionSettings()
    assert settings.VISION_DESCRIBE is False
    assert settings.VISION_RECEIPT_MODEL == ""
    assert settings.VISION_DESCRIBE_PER_USER_PER_DAY == 30
    with pytest.raises(ValueError):
        VisionSettings(VISION_DESCRIBE_PER_USER_PER_DAY=501)
