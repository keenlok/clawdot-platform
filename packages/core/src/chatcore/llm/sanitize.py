"""Remove tool-call syntax that a provider returned as ordinary assistant text."""

from __future__ import annotations

import re

_BLOCKS = (
    re.compile(
        r"<｜DSML｜\s*(?:function_)?calls\s*>.*?(?:</｜DSML｜\s*(?:function_)?calls\s*>|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"<｜DSML｜\s*invoke(?=[\s>])(?:[^>\n]*\Z|[^>]*>.*?(?:</｜DSML｜\s*invoke\s*>|\Z))",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"<tool_calls(?=[\s>])(?:[^>\n]*\Z|[^>]*>.*?(?:</tool_calls\s*>|\Z))",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"<tool_call(?=[\s>])(?:[^>\n]*\Z|[^>]*>.*?(?:</tool_call\s*>|\Z))",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"<function_calls(?=[\s>])(?:[^>\n]*\Z|[^>]*>.*?(?:</function_calls\s*>|\Z))",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"<invoke(?=[\s>])(?:[^>\n]*\Z|[^>]*>.*?(?:</invoke\s*>|\Z))", re.IGNORECASE | re.DOTALL
    ),
    re.compile(
        r"<([｜|])tool[_▁]calls[_▁]begin\1>.*?(?:<\1tool[_▁]calls[_▁]end\1>|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"<([｜|])tool[_▁]call[_▁]begin\1>.*?(?:<\1tool[_▁]call[_▁]end\1>|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"<([｜|])tool[_▁]outputs[_▁]begin\1>.*?(?:<\1tool[_▁]outputs[_▁]end\1>|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
)
_DSML_TAG = re.compile(r"</?｜DSML｜[^>]*(?:>|\Z)", re.IGNORECASE | re.DOTALL)
_SPECIAL_TOKEN = re.compile(r"<｜[^｜\n]{1,40}｜>", re.DOTALL)
# A special token cut off at the end of the text, e.g. "<｜tool▁calls▁be".
_PARTIAL_TOKEN = re.compile(r"<｜[^｜<>\n]{0,40}\Z")
_MANY_NEWLINES = re.compile(r"\n{3,}")


def strip_tool_markup(text: str) -> tuple[str, bool]:
    """Remove leaked tool-call markup. Returns (clean_text, stripped)."""
    clean = text
    for pattern in _BLOCKS:
        clean = pattern.sub("", clean)
    clean = _DSML_TAG.sub("", clean)
    clean = _SPECIAL_TOKEN.sub("", clean)
    clean = _PARTIAL_TOKEN.sub("", clean)
    stripped = clean != text
    if not stripped:
        return text, False
    return _MANY_NEWLINES.sub("\n\n", clean).strip(), True
