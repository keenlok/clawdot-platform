from __future__ import annotations

import httpx
import pytest
from chatcore.security import net_guard
from chatcore_web import maps


def test_parses_google_search_place_facts() -> None:
    place = maps.parse_map_url(
        "https://www.google.com/search?q=Ah+Hua+Cafe&kgmid=/g/11abc&shndl=30"
    )
    assert place == maps.MapPlace("Ah Hua Cafe", "place", "/g/11abc", None, None, None)


def test_parses_maps_place_and_prefers_data_coordinates() -> None:
    place = maps.parse_map_url(
        "https://www.google.com/maps/place/Ah+Hua+Cafe/@1.3001,103.8002,17z/"
        "data=!3m1!4b1!4m6!3m5!1s0x31da19:0x5f1c!8m2!3d1.3005!4d103.8009"
    )
    assert place is not None
    assert place.name == "Ah Hua Cafe"
    assert place.place_id == "0x31da19:0x5f1c"
    assert (place.lat, place.lng) == (1.3005, 103.8009)


def test_decodes_literal_plus_and_plus_space_in_path_name() -> None:
    place = maps.parse_map_url("https://www.google.com/maps/place/C%2B%2B+Cafe")
    assert place is not None and place.name == "C++ Cafe"


def test_numeric_query_coordinates_are_used_with_path_name() -> None:
    place = maps.parse_map_url("https://www.google.com/maps/place/Cafe?q=1.3,103.8")
    assert place is not None
    assert place.name == "Cafe"
    assert (place.lat, place.lng) == (1.3, 103.8)


@pytest.mark.parametrize(
    ("url", "coords"),
    [
        ("https://www.google.com/maps/@1.2,2.3,8z", (1.2, 2.3)),
        ("https://www.google.com/maps?ll=1.2,2.3", (1.2, 2.3)),
        ("https://www.google.com/search?q=1.2,2.3", (1.2, 2.3)),
        ("https://www.google.com/search?q=91,2", None),
    ],
)
def test_coordinate_sources_and_validation(url: str, coords: tuple[float, float] | None) -> None:
    place = maps.parse_map_url(url)
    assert place is not None if coords else place is None
    if place:
        assert (place.lat, place.lng) == coords


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/doodles",
        "https://docs.google.com/document/d/x",
        "https://www.google.com/",
    ],
)
def test_non_maps_google_pages_do_not_parse(url: str) -> None:
    assert maps.parse_map_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://google.evil.com/maps/place/x",
        "https://evilgoogle.com/maps/place/x",
        "https://google.com.evil.net/maps/place/x",
        "https://share.google.evil.com/abc",
    ],
)
def test_lookalike_hosts_are_not_maps(url: str) -> None:
    assert not maps.is_map_link(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://share.google/abc",
        "https://maps.app.goo.gl/abc",
        "https://goo.gl/maps/abc",
        "https://maps.google.com.sg/maps/place/x",
        "https://www.google.co.uk/maps/place/x",
    ],
)
def test_supported_google_map_hosts_are_recognized(url: str) -> None:
    assert maps.is_map_link(url)


def test_sanitizes_and_caps_name() -> None:
    place = maps.parse_map_url("https://www.google.com/maps/place/A%00%01+Cafe/" + "x" * 300)
    assert place is not None
    assert "\x00" not in place.name
    assert "\x01" not in place.name
    long_name = maps.parse_map_url("https://www.google.com/maps/place/" + "A" * 300)
    assert long_name is not None and len(long_name.name) == 200


async def test_share_google_redirect_stops_at_google_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.headers["host"] == "share.google":
            return httpx.Response(
                302, headers={"location": "https://www.google.com/share.google?q=abc"}
            )
        return httpx.Response(
            301,
            headers={"location": "https://www.google.com/search?q=Ah+Hua+Cafe&kgmid=/g/11abc"},
        )

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    place = await maps.resolve_map_link("https://share.google/abc", timeout_s=2)
    assert place is not None and place.name == "Ah Hua Cafe" and place.kgmid == "/g/11abc"
    assert len(requested) == 2


async def test_full_maps_place_link_needs_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("network must not be used")

    monkeypatch.setattr(net_guard, "resolve_redirects", unexpected)
    place = await maps.resolve_map_link(
        "https://www.google.com/maps/place/Ah+Hua+Cafe/@1.3,103.8,17z", timeout_s=1
    )
    assert place is not None and place.name == "Ah Hua Cafe"


async def test_maps_app_redirect_stops_before_place_page(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://www.google.com/maps/place/Ah+Hua+Cafe/@1.3,103.8,17z"},
        )

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(net_guard, "_make_transport", lambda: httpx.MockTransport(handler))
    place = await maps.resolve_map_link("https://maps.app.goo.gl/xyz", timeout_s=2)
    assert place is not None and place.name == "Ah Hua Cafe"
    assert (place.lat, place.lng) == (1.3, 103.8)
    assert len(requested) == 1
