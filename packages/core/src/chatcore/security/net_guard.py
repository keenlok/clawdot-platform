"""Fetch public web pages without allowing access to private networks."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BlockedURL(ValueError):
    """The URL points to a destination that is not safe to fetch."""


class FetchError(RuntimeError):
    """The remote page could not be fetched safely."""


class NetGuardSettings(BaseSettings):
    """Deployment additions for network ranges belonging to this host."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)
    extra_blocked: str = Field(default="", validation_alias="NET_GUARD_EXTRA_BLOCKED")
    prefer_ipv4: bool = Field(default=True, validation_alias="NET_PREFER_IPV4")
    connect_attempts: int = Field(default=3, validation_alias="NET_CONNECT_ATTEMPTS")


@dataclass(frozen=True, slots=True)
class FetchResult:
    final_url: str
    status: int
    content_type: str
    body: bytes
    truncated: bool


@dataclass(frozen=True, slots=True)
class RedirectResult:
    final_url: str
    status: int | None
    hops: int


def failure_cause(exc: BaseException) -> str:
    """Return the class name of the chained cause, or ``none`` when unchained."""
    return type(exc.__cause__).__name__ if exc.__cause__ is not None else "none"


_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fec0::/10",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2001:db8::/32",
        "::ffff:0:0/96",
    )
)
_BLOCKED_HOSTS = {"localhost", "postgresql", "elasticsearch", "sb-bot-valkey"}


def _normalize_host(host: str) -> str:
    try:
        return host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise BlockedURL("The URL hostname is invalid.") from exc


def _parse_url(url: str) -> tuple[str, str, int, str]:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            raise BlockedURL("Only HTTP and HTTPS URLs can be fetched.")
        if parts.username is not None or parts.password is not None:
            raise BlockedURL("URLs with credentials cannot be fetched.")
        host = parts.hostname
        if not host:
            raise BlockedURL("The URL must include a hostname.")
        host = _normalize_host(host)
        explicit_port = parts.port
        port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
        if port not in {80, 443}:
            raise BlockedURL("Only ports 80 and 443 can be fetched.")
        if (
            host in _BLOCKED_HOSTS
            or host.endswith(".localhost")
            or ("." not in host and _literal_ip(host) is None)
        ):
            raise BlockedURL("This hostname is not allowed.")
        path = urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))
        return scheme, host, port, path
    except ValueError as exc:
        if isinstance(exc, BlockedURL):
            raise
        raise BlockedURL("The URL is invalid.") from exc


def parse_https_destination(authority_or_url: str) -> tuple[str, int]:
    """Parse a public HTTPS destination or CONNECT authority using the fetch policy."""
    try:
        if len(authority_or_url) > 2048:
            raise BlockedURL("The destination is too long.")
        if "://" in authority_or_url:
            parts = urlsplit(authority_or_url)
            if parts.scheme.lower() != "https":
                raise BlockedURL("Only HTTPS destinations can be fetched.")
            if parts.username is not None or parts.password is not None:
                raise BlockedURL("Destinations with credentials cannot be fetched.")
        else:
            if any(char in authority_or_url for char in "/?#"):
                raise BlockedURL("The destination authority is invalid.")
            parts = urlsplit(f"https://{authority_or_url}")
            if parts.username is not None or parts.password is not None:
                raise BlockedURL("Destinations with credentials cannot be fetched.")
        host = parts.hostname
        if not host:
            raise BlockedURL("The destination must include a hostname.")
        if parts.netloc.endswith(":"):
            raise BlockedURL("The destination port is invalid.")
        host = _normalize_host(host)
        port = parts.port if parts.port is not None else 443
        if port != 443:
            raise BlockedURL("Only port 443 can be fetched by the renderer.")
        blocked = get_extra_blocked()
        _check_hostname(host, blocked)
        literal = _literal_ip(host)
        if literal is not None:
            _check_ip(str(literal), blocked)
        return host, port
    except ValueError as exc:
        if isinstance(exc, BlockedURL):
            raise
        raise BlockedURL("The destination is invalid.") from exc


def get_extra_blocked(extra_blocked: tuple[str, ...] | list[str] = ()) -> tuple[str, ...]:
    """Return caller-provided and deployment-configured network guard entries."""
    settings = NetGuardSettings()
    return tuple(extra_blocked) + tuple(
        item.strip() for item in settings.extra_blocked.split(",") if item.strip()
    )


def check_public_ip(address: str, extra_blocked: tuple[str, ...] | list[str] = ()) -> None:
    """Raise the network guard's standard error if an address is not public."""
    _check_ip(address, get_extra_blocked(extra_blocked))


async def resolve_public(
    host: str, port: int, extra_blocked: tuple[str, ...] | list[str] = ()
) -> list[str]:
    """Resolve a host once and reject it unless every answer is a public address."""
    blocked = get_extra_blocked(extra_blocked)
    normalized = _normalize_host(host)
    _check_hostname(normalized, blocked)
    literal = _literal_ip(normalized)
    addresses = [str(literal)] if literal is not None else await _resolve(normalized, port)
    if not addresses:
        raise FetchError("The hostname returned no addresses.")
    for address in addresses:
        _check_ip(address, blocked)
    return addresses


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        # inet_aton intentionally recognizes decimal, octal, and abbreviated IPv4 forms.
        try:
            raw = socket.inet_aton(host)
            return ipaddress.IPv4Address(raw)
        except OSError:
            return None


def _check_ip(address: str, extra_blocked: tuple[str, ...]) -> None:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        raise FetchError("The hostname returned an invalid address.") from exc
    embedded_v4: list[ipaddress.IPv4Address] = []
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            embedded_v4.append(ip.ipv4_mapped)
        if ip.sixtofour:
            embedded_v4.append(ip.sixtofour)
        if ip.teredo:
            embedded_v4.extend(ip.teredo)
        nat64_ranges = (
            ipaddress.ip_network("64:ff9b::/96"),
            ipaddress.ip_network("64:ff9b:1::/48"),
        )
        if any(ip in network for network in nat64_ranges):
            embedded_v4.append(ipaddress.IPv4Address(ip.packed[-4:]))
        if ip in ipaddress.ip_network("::/96"):
            embedded_v4.append(ipaddress.IPv4Address(ip.packed[-4:]))

    for embedded in embedded_v4:
        _check_ipv4(embedded, extra_blocked)

    for raw in (*[str(n) for n in _BLOCKED_NETWORKS], *extra_blocked):
        if "/" in raw:
            try:
                network = ipaddress.ip_network(raw, strict=False)
            except ValueError:
                raise BlockedURL("Network guard configuration is invalid.") from None
            if ip.version == network.version and ip in network:
                raise BlockedURL("The URL resolves to a blocked network.")
        else:
            try:
                entry_ip = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if ip == entry_ip:
                raise BlockedURL("The URL resolves to a blocked host.")
    if not ip.is_global:
        raise BlockedURL("The URL resolves to a non-public address.")


def _check_ipv4(ip: ipaddress.IPv4Address, extra_blocked: tuple[str, ...]) -> None:
    for raw in (*[str(n) for n in _BLOCKED_NETWORKS], *extra_blocked):
        if "/" in raw:
            try:
                network = ipaddress.ip_network(raw, strict=False)
            except ValueError:
                raise BlockedURL("Network guard configuration is invalid.") from None
            if network.version == 4 and ip in network:
                raise BlockedURL("The URL contains an IPv4 address in a blocked network.")
        else:
            try:
                entry_ip = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if ip == entry_ip:
                raise BlockedURL("The URL contains a blocked IPv4 address.")
    if not ip.is_global:
        raise BlockedURL("The URL contains a non-public IPv4 address.")


def _check_hostname(host: str, extra_blocked: tuple[str, ...]) -> None:
    if (
        host in _BLOCKED_HOSTS
        or host.endswith(".localhost")
        or ("." not in host and _literal_ip(host) is None)
    ):
        raise BlockedURL("This hostname is not allowed.")
    for item in extra_blocked:
        if "/" not in item and _normalize_host(item) == host:
            raise BlockedURL("This hostname is blocked by network guard settings.")


async def _resolve(host: str, port: int) -> list[str]:
    def lookup() -> list[str]:
        addresses = list(
            dict.fromkeys(
                item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            )
        )
        return _order_addresses(addresses, NetGuardSettings().prefer_ipv4)

    try:
        return await asyncio.to_thread(lookup)
    except OSError as exc:
        raise FetchError("The hostname could not be resolved.") from exc


def _order_addresses(addresses: list[str], prefer_ipv4: bool) -> list[str]:
    if not prefer_ipv4:
        return list(addresses)
    ipv4 = [
        address
        for address in addresses
        if ipaddress.ip_address(address.split("%", 1)[0]).version == 4
    ]
    ipv6 = [
        address
        for address in addresses
        if ipaddress.ip_address(address.split("%", 1)[0]).version == 6
    ]
    return ipv4 + ipv6


def _make_transport() -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(retries=0)


def _mime_allowed(content_type: str, allowed_types: tuple[str, ...]) -> bool:
    mime = content_type.split(";", 1)[0].strip().lower()
    aliases = {"html": "text/html", "pdf": "application/pdf", "json": "application/json"}
    for item in allowed_types:
        normalized = aliases.get(item.lower(), item.lower())
        matches = (
            mime.startswith(normalized[:-1]) if normalized.endswith("*") else mime == normalized
        )
        if matches:
            return True
    return False


async def safe_get(
    url: str,
    *,
    max_bytes: int,
    timeout_s: float,
    allowed_types: tuple[str, ...] | list[str],
    extra_blocked: tuple[str, ...] | list[str] = (),
) -> FetchResult:
    """Fetch an allowlisted response type after resolving and pinning a public IP."""
    if max_bytes < 0 or timeout_s <= 0:
        raise ValueError("max_bytes must be non-negative and timeout_s must be positive")
    blocked = get_extra_blocked(extra_blocked)
    attempts = max(1, min(NetGuardSettings().connect_attempts, 5))
    current_url = url
    deadline = asyncio.get_running_loop().time() + timeout_s
    for redirect_count in range(4):
        try:
            async with _open_hop(current_url, deadline, attempts, blocked) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirect_count == 3:
                        raise FetchError("The page redirected too many times.")
                    current_url = urljoin(current_url, location)
                    continue
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding != "identity":
                    raise FetchError("Compressed page responses are not allowed.")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                if not _mime_allowed(content_type, tuple(allowed_types)):
                    raise FetchError("The page returned a content type that is not allowed.")
                chunks = bytearray()
                truncated = False
                async for chunk in response.aiter_bytes():
                    available = max_bytes - len(chunks)
                    chunks.extend(chunk[:available])
                    if len(chunk) > available:
                        truncated = True
                        break
                return FetchResult(
                    current_url, response.status_code, content_type, bytes(chunks), truncated
                )
        except (BlockedURL, FetchError):
            raise
        except TimeoutError as exc:
            raise FetchError("The page request timed out.") from exc
        except httpx.HTTPError as exc:
            raise FetchError("The page request failed.") from exc
    raise FetchError("The page redirected too many times.")


@asynccontextmanager
async def _open_hop(
    current_url: str, deadline: float, attempts: int, blocked: tuple[str, ...]
) -> AsyncIterator[httpx.Response]:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise FetchError("The page request timed out.")
    scheme, host, port, request_url = _parse_url(current_url)
    _check_hostname(host, blocked)
    literal = _literal_ip(host)
    try:
        addresses = (
            [str(literal)] if literal else await asyncio.wait_for(_resolve(host, port), remaining)
        )
    except TimeoutError as exc:
        raise FetchError("The hostname lookup timed out.") from exc
    if not addresses:
        raise FetchError("The hostname returned no addresses.")
    for address in addresses:
        _check_ip(address, blocked)
    original = urlsplit(current_url)
    request_parts = urlsplit(request_url)
    host_header = f"[{host}]" if ":" in host else host
    if original.port is not None:
        host_header += f":{port}"
    headers = {"Host": host_header, "User-Agent": "Mozilla/5.0 (compatible; sb_bot/0.1)"}
    candidates = addresses[:attempts]
    last_error: httpx.HTTPError | None = None
    for index, address in enumerate(candidates):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise FetchError("The page request timed out.")
        attempts_left = len(candidates) - index
        connect_timeout = min(remaining, max(1.0, remaining / attempts_left))
        ip = ipaddress.ip_address(address)
        authority = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
        pinned_url = urlunsplit(
            (scheme, f"{authority}:{port}", request_parts.path, request_parts.query, "")
        )
        async with httpx.AsyncClient(
            transport=_make_transport(),
            follow_redirects=False,
            timeout=httpx.Timeout(remaining, connect=connect_timeout),
            cookies=None,
            auth=None,
            trust_env=False,
        ) as client:
            stream = client.stream(
                "GET",
                pinned_url,
                headers={**headers, "Accept-Encoding": "identity"},
                extensions={"sni_hostname": host},
            )
            async with asyncio.timeout(remaining):
                try:
                    response = await stream.__aenter__()
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    last_error = exc
                    if index + 1 == len(candidates):
                        raise FetchError("The site could not be reached.") from exc
                    continue
                except httpx.HTTPError as exc:
                    raise FetchError("The page request failed.") from exc
                try:
                    yield response
                finally:
                    await stream.__aexit__(None, None, None)
            return
    if last_error:
        raise FetchError("The site could not be reached.") from last_error


async def resolve_redirects(
    url: str,
    *,
    timeout_s: float,
    stop_at: Callable[[str], bool],
    max_redirects: int = 5,
    extra_blocked: tuple[str, ...] | list[str] = (),
) -> RedirectResult:
    """Follow guarded redirects until a response or caller-selected URL is reached."""
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    blocked = get_extra_blocked(extra_blocked)
    attempts = max(1, min(NetGuardSettings().connect_attempts, 5))
    deadline = asyncio.get_running_loop().time() + timeout_s
    current_url = url
    hops = 0
    last_status: int | None = None
    while True:
        if hops and stop_at(current_url):
            return RedirectResult(current_url, None, hops)
        try:
            async with _open_hop(current_url, deadline, attempts, blocked) as response:
                last_status = response.status_code
                if response.status_code not in {301, 302, 303, 307, 308}:
                    return RedirectResult(current_url, last_status, hops)
                location = response.headers.get("location")
                if not location:
                    return RedirectResult(current_url, last_status, hops)
                if hops >= max_redirects:
                    raise FetchError("The page redirected too many times.")
                current_url = urljoin(current_url, location)
                hops += 1
        except (BlockedURL, FetchError):
            raise
        except TimeoutError as exc:
            raise FetchError("The page request timed out.") from exc
        except httpx.HTTPError as exc:
            raise FetchError("The page request failed.") from exc
