"""Search the public web and read pages returned by a human or search result."""

from chatcore_web.plugin import WebPlugin
from chatcore_web.plugin import _extract as _extract  # used by tests
from chatcore_web.plugin import _normalize_url as _normalize_url  # used by chatcore_research
from chatcore_web.settings import WebSettings

__all__ = ["WebPlugin", "WebSettings"]
