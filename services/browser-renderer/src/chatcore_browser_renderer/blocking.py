from __future__ import annotations

from urllib.parse import urlsplit

MARKERS = (
    "verify you are human",
    "are you a robot",
    "captcha",
    "just a moment",
    "attention required",
    "access denied",
    "request unsuccessful",
    "pardon our interruption",
    "unusual traffic",
    "checking your browser",
    "enable javascript and cookies to continue",
)
CHALLENGE_STATUSES = {401, 403, 429, 451, 503}


def _is_challenge_frame(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    path = parsed.path.casefold()
    if host == "challenges.cloudflare.com" or host.endswith(".challenges.cloudflare.com"):
        return True
    google_recaptcha_path = (host == "google.com" or host.endswith(".google.com")) and any(
        path == f"/recaptcha/api2/{endpoint}" or path.startswith(f"/recaptcha/api2/{endpoint}/")
        for endpoint in ("anchor", "bframe")
    )
    if google_recaptcha_path:
        return True
    hcaptcha_host = host == "hcaptcha.com" or host.endswith(".hcaptcha.com")
    if hcaptcha_host and (path == "/captcha" or path.startswith("/captcha/")):
        return True
    path_parts = set(path.split("/"))
    awswaf_host = host == "awswaf.com" or host.endswith(".awswaf.com")
    if awswaf_host and path_parts.intersection({"challenge", "captcha"}):
        return True
    datadome_host = any(
        host == domain or host.endswith("." + domain)
        for domain in ("datadome.co", "captcha-delivery.com")
    )
    return datadome_host and bool(path_parts.intersection({"captcha", "challenge"}))


def detect_block(
    status: int, title: str, text: str, frame_urls: list[str] | tuple[str, ...]
) -> bool:
    """Conservatively identify short anti-automation walls without keyword-only false positives."""
    body = f"{title}\n{text}".casefold()
    has_challenge_frame = any(_is_challenge_frame(url) for url in frame_urls)
    if has_challenge_frame:
        return True
    return (
        status in CHALLENGE_STATUSES
        and len(text) < 1500
        or (len(text) < 1500 and any(marker in body for marker in MARKERS))
    )
