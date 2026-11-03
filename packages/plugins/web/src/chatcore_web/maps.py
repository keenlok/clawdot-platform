"""Parse useful place facts from Google Maps and share-link redirects."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote_plus, urlsplit

from chatcore.security import net_guard

_GOOGLE_HOST = re.compile(
    r"^(?:www\.|maps\.)?google\.(?:com|[a-z]{2}|com\.[a-z]{2}|co\.[a-z]{2})$", re.I
)
_ID = re.compile(r"[A-Za-z0-9_/:.\-]+")
_FLOAT = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_COORD = rf"({_FLOAT}),\s*({_FLOAT})"


@dataclass(frozen=True, slots=True)
class MapPlace:
    name: str | None
    kind: str
    kgmid: str | None
    place_id: str | None
    lat: float | None
    lng: float | None


def _parts(url: str):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        return parts, host
    except (ValueError, UnicodeError):
        return None, ""


def _google_host(host: str) -> bool:
    return bool(_GOOGLE_HOST.fullmatch(host))


def is_map_link(url: str) -> bool:
    parts, host = _parts(url)
    if parts is None or parts.scheme.lower() not in {"http", "https"}:
        return False
    path = parts.path.lower()
    if host in {"share.google", "maps.app.goo.gl"}:
        return True
    if host == "goo.gl":
        return path.startswith("/maps")
    if host.startswith("maps.google.") and _google_host(host):
        return True
    return bool(_google_host(host) and path.startswith("/maps"))


def is_google_target(url: str) -> bool:
    parts, host = _parts(url)
    return bool(
        parts is not None
        and parts.scheme.lower() in {"http", "https"}
        and _google_host(host)
        and (parts.path.lower() == "/search" or parts.path.lower().startswith("/maps"))
    )


def _clean_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
    cleaned = " ".join(cleaned.split()).strip()[:200]
    return cleaned or None


def _clean_id(value: str | None) -> str | None:
    if not value:
        return None
    value = value[:100]
    return value if _ID.fullmatch(value) else None


def _coordinates(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    match = re.fullmatch(_COORD, value.strip())
    if not match:
        return None
    lat, lng = map(float, match.groups())
    return _bounded_coordinates(lat, lng)


def _bounded_coordinates(lat: float, lng: float) -> tuple[float, float] | None:
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return lat, lng
    return None


def parse_map_url(url: str) -> MapPlace | None:
    parts, host = _parts(url)
    if (
        parts is None
        or parts.scheme.lower() not in {"http", "https"}
        or not _google_host(host)
        or not (parts.path.lower() == "/search" or parts.path.lower().startswith("/maps"))
    ):
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    path = parts.path
    segments = [segment for segment in path.split("/") if segment]
    name: str | None = None
    kind = "search"
    for prefix in ("place", "search"):
        if len(segments) >= 2 and segments[0].lower() == "maps" and segments[1].lower() == prefix:
            name = unquote_plus(segments[2]) if len(segments) >= 3 else None
            kind = "place" if prefix == "place" else "search"
            break
    raw_query = next((query[key][0] for key in ("q", "query") if query.get(key)), None)
    query_match = re.fullmatch(_COORD, raw_query.strip()) if raw_query else None
    query_is_coordinates = query_match is not None
    query_coordinates = (
        _bounded_coordinates(*map(float, query_match.groups())) if query_match else None
    )
    if name is None and raw_query and not query_is_coordinates:
        name = raw_query

    kgmid = _clean_id(query.get("kgmid", [None])[0])
    place_id = next(
        (
            _clean_id(query[key][0])
            for key in ("ftid", "place_id", "query_place_id", "cid")
            if query.get(key)
        ),
        None,
    )
    data_values = query.get("data", [])
    searchable = path + " " + " ".join(data_values)
    if place_id is None:
        match = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", searchable, re.I)
        if match:
            place_id = _clean_id(match.group(1))

    coord = None
    match = re.search(rf"!3d({_FLOAT})!4d({_FLOAT})", searchable)
    if match:
        coord = _coordinates(f"{match.group(1)},{match.group(2)}")
    if coord is None:
        match = re.search(r"@(" + _COORD + r")", path)
        if match:
            coord = _coordinates(match.group(1))
    if coord is None:
        coord = _coordinates(query.get("ll", [None])[0])
    if coord is None:
        coord = query_coordinates

    if kgmid or place_id:
        kind = "place"
    clean_name = _clean_name(name)
    if not any((clean_name, kgmid, place_id, coord)):
        return None
    return MapPlace(
        clean_name, kind, kgmid, place_id, coord[0] if coord else None, coord[1] if coord else None
    )


async def resolve_map_link(url: str, *, timeout_s: float) -> MapPlace | None:
    place = parse_map_url(url)
    if place is not None:
        return place
    result = await net_guard.resolve_redirects(url, timeout_s=timeout_s, stop_at=is_google_target)
    return parse_map_url(result.final_url)


def format_place(place: MapPlace) -> str:
    lines = ["Google Maps link"]
    if place.name:
        lines.append(f"{'Place' if place.kind == 'place' else 'Search text'}: {place.name}")
    if place.kgmid:
        lines.append(f"Google entity id: {place.kgmid}")
    if place.place_id:
        lines.append(f"Place id: {place.place_id}")
    if place.lat is not None and place.lng is not None:
        lines.append(f"Coordinates: {place.lat:.5f}, {place.lng:.5f}")
    lines.append(
        "This link only identifies the place. Use search_web with the place name "
        "(plus area if known) for address, hours and reviews."
    )
    return "\n".join(lines)


__all__ = [
    "MapPlace",
    "format_place",
    "is_google_target",
    "is_map_link",
    "parse_map_url",
    "resolve_map_link",
]
