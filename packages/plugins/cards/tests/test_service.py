import pytest
from chatcore.cards import CardError
from chatcore_cards.service import CardQuotaError, CardService
from chatcore_cards.settings import CardsSettings


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class Renderer:
    def __init__(self, result=None, error=None):
        self.result = result or [b"png"]
        self.error = error
        self.calls = 0

    async def render(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    async def aclose(self):
        pass


def settings(**values):
    return CardsSettings(CARDS_ENABLED=True, CARDS_RENDER_URL="http://renderer", **values)


async def test_quota_is_per_user_and_charged_only_to_that_users_attempts():
    clock, renderer = Clock(), Renderer()
    service = CardService(settings(CARDS_MAX_PER_USER_HOUR=1), client=renderer, clock=clock)
    try:
        assert await service.render("card", user_key="user-a") == [b"png"]
        with pytest.raises(CardQuotaError) as quota:
            await service.render("card", user_key="user-a")
        assert quota.value.code == "quota"
        assert await service.render("card", user_key="user-b") == [b"png"]
        assert renderer.calls == 2
    finally:
        await service.aclose()


async def test_rolling_window_expires_only_attempts_older_than_one_hour():
    clock, renderer = Clock(), Renderer()
    service = CardService(settings(CARDS_MAX_PER_USER_HOUR=2), client=renderer, clock=clock)
    try:
        await service.render("card", user_key="user")
        clock.value = 1800
        await service.render("card", user_key="user")
        clock.value = 1801
        with pytest.raises(CardQuotaError):
            await service.render("card", user_key="user")
        assert renderer.calls == 2
        clock.value = 3601
        await service.render("card", user_key="user")
        assert renderer.calls == 3
    finally:
        await service.aclose()


@pytest.mark.parametrize("code", ["invalid", "busy", "too_large", "timeout", "unavailable"])
async def test_renderer_card_errors_pass_through(code):
    service = CardService(settings(), client=Renderer(error=CardError(code)), clock=Clock())
    try:
        with pytest.raises(CardError) as error:
            await service.render("card", user_key="user")
        assert error.value.code == code
    finally:
        await service.aclose()


async def test_unexpected_renderer_errors_map_to_unavailable():
    service = CardService(settings(), client=Renderer(error=RuntimeError("private")), clock=Clock())
    try:
        with pytest.raises(CardError) as error:
            await service.render("card", user_key="user")
        assert error.value.code == "unavailable"
        assert "private" not in str(error.value)
    finally:
        await service.aclose()


async def test_failed_render_consumes_hourly_quota():
    service = CardService(
        settings(CARDS_MAX_PER_USER_HOUR=1),
        client=Renderer(error=CardError("busy")),
        clock=Clock(),
    )
    try:
        with pytest.raises(CardError) as first:
            await service.render("card", user_key="user")
        assert first.value.code == "busy"
        with pytest.raises(CardQuotaError) as second:
            await service.render("card", user_key="user")
        assert second.value.code == "quota"
    finally:
        await service.aclose()


async def test_service_logs_only_safe_error_code(caplog):
    service = CardService(
        settings(),
        client=Renderer(error=CardError("unavailable", "PRIVATE CARD TEXT")),
        clock=Clock(),
    )
    try:
        with pytest.raises(CardError):
            await service.render("PRIVATE CARD TEXT", user_key="user")
        assert "card render failed code=unavailable" in caplog.text
        assert "PRIVATE CARD TEXT" not in caplog.text
    finally:
        await service.aclose()


def test_settings_bounds_and_renderer_url_validation():
    invalid = (
        {"CARDS_TIMEOUT_SECONDS": 0},
        {"CARDS_TIMEOUT_SECONDS": 61},
        {"CARDS_MAX_TEXT_CHARS": 999},
        {"CARDS_MAX_TEXT_CHARS": 12001},
        {"CARDS_FOOTER": "x" * 61},
        {"CARDS_FOOTER": "bad\nfooter"},
        {"CARDS_RENDER_URL": "http://user@renderer"},
        {"CARDS_RENDER_URL": "https:///path"},
    )
    for values in invalid:
        with pytest.raises(ValueError):
            CardsSettings(**values)
    assert CardsSettings(
        CARDS_TIMEOUT_SECONDS=60, CARDS_MAX_TEXT_CHARS=12000, CARDS_FOOTER="x" * 60
    )


def test_available_requires_enabled_url_and_positive_cap():
    renderer = Renderer()
    assert not CardService(CardsSettings(), client=renderer).available
    assert not CardService(CardsSettings(CARDS_ENABLED=True), client=renderer).available
    assert not CardService(settings(CARDS_MAX_PER_USER_HOUR=0), client=renderer).available
    assert CardService(settings(), client=renderer).available
