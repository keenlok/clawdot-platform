"""Exact initial Docs UI resource hosts; live qualification must narrow/confirm this set."""

RESOURCE_HOSTS = frozenset(
    {
        "accounts.google.com",
        "apis.google.com",
        "clients6.google.com",
        "content.googleapis.com",
        "docs.google.com",
        "docs.googleusercontent.com",
        "drive.google.com",
        "fonts.gstatic.com",
        "lh3.googleusercontent.com",
        "play.google.com",
        "ssl.gstatic.com",
        "www.gstatic.com",
    }
)


def allowed_cookie_domain(domain: str) -> bool:
    value = domain.lstrip(".").casefold()
    return (
        value == "google.com"
        or value.endswith(".google.com")
        or value == "googleusercontent.com"
        or value.endswith(".googleusercontent.com")
    )
