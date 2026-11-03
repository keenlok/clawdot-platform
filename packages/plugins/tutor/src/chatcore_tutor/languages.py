"""Fixed language and romanisation names used by tutor profiles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Language:
    name: str
    aliases: tuple[str, ...]
    romanisation: str | None


LANGUAGES = (
    Language("English", ("en",), None),
    Language("Japanese", ("ja", "日本語"), "Hepburn romaji"),
    Language(
        "Mandarin Chinese",
        ("chinese", "mandarin", "zh", "中文", "普通话", "华语"),
        "Hanyu Pinyin",
    ),
    Language("Cantonese", ("yue", "粤语", "廣東話"), "Jyutping"),
    Language("Korean", ("ko", "한국어"), "Revised Romanization"),
    Language("Thai", ("th", "ภาษาไทย"), "RTGS"),
    Language("Malay", ("ms", "bahasa melayu"), None),
    Language("Indonesian", ("id", "bahasa indonesia"), None),
    Language("Vietnamese", ("vi", "tiếng việt"), None),
    Language("Tamil", ("ta", "தமிழ்"), "ISO 15919"),
    Language("Hindi", ("hi", "हिन्दी"), "IAST"),
    Language("Tagalog", ("tl", "filipino"), None),
    Language("French", ("fr",), None),
    Language("Spanish", ("es",), None),
    Language("German", ("de",), None),
    Language("Italian", ("it",), None),
    Language("Portuguese", ("pt",), None),
    Language("Dutch", ("nl",), None),
    Language("Russian", ("ru", "русский"), "scholarly transliteration"),
    Language("Arabic", ("ar", "العربية"), "transliteration"),
)

_LANGUAGE_BY_ALIAS = {
    alias.casefold(): language
    for language in LANGUAGES
    for alias in (language.name, *language.aliases)
}
LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")


def lookup_language(token: str) -> Language | None:
    return _LANGUAGE_BY_ALIAS.get(token.strip().casefold())


def parse_level(token: str) -> str | None:
    normalized = token.strip().upper()
    return normalized if normalized in LEVELS else None
