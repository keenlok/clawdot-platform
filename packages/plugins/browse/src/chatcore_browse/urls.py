"""URL normalization used for provenance and cache keys."""

from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    port = parts.port
    authority = f"[{host}]" if ":" in host else host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        authority += f":{port}"
    return urlunsplit((scheme, authority, parts.path or "/", parts.query, ""))


def upgrade_http_url(url: str) -> str:
    """Upgrade a validated ordinary HTTP URL to HTTPS, preserving its path and query."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "http":
        return url
    host = parts.hostname or ""
    authority = f"[{host}]" if ":" in host else host
    if parts.port not in {None, 80}:
        authority += f":{parts.port}"
    return urlunsplit(("https", authority, parts.path, parts.query, ""))
