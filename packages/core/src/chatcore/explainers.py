"""Validated, packaged evidence about framework and plugin behavior."""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chatcore.plugin import Plugin

MAX_DOCUMENT_BYTES = 24 * 1024
SECTION_NAMES = ("Overview", "Workflow", "Storage", "Safety", "Limitations", "Verification")
SECTION_IDS = tuple(name.lower() for name in SECTION_NAMES)
_TOPIC = re.compile(r"[a-z][a-z0-9_]{0,47}")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
BUILTIN_PLUGINS = (
    "browse",
    "expenses",
    "extensions",
    "google",
    "image",
    "jobs",
    "memory",
    "polls",
    "research",
    "search",
    "stocks",
    "tutor",
    "voice",
    "web",
)


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True, slots=True)
class ExplanationSource:
    repository: str
    revision: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,39}/[A-Za-z0-9_.-]{1,100}", self.repository):
            raise ValueError("Invalid public source repository")
        if len(self.revision) > 64 or not re.fullmatch(
            r"(?:[a-f0-9]{40}|v[0-9]+\.[0-9]+\.[0-9]+)", self.revision
        ):
            raise ValueError("Source revision must be a release tag or full commit SHA")


@dataclass(frozen=True, slots=True)
class ExplainerSpec:
    topic: str
    title: str
    summary: str
    aliases: tuple[str, ...]
    package: str
    owner: str
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExplainerDocument:
    spec: ExplainerSpec
    sections: tuple[tuple[str, str], ...]
    sha256: str


def parse_explainer(
    text: str, *, package: str, owner: str, tools: tuple[str, ...]
) -> ExplainerDocument:
    """Parse one source; headings inside fenced examples remain ordinary section text."""
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ValueError("Explainer exceeds document size limit")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "+++":
        raise ValueError("Missing TOML front matter")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "+++"), None)
    if end is None:
        raise ValueError("Unclosed TOML front matter")
    metadata = tomllib.loads("".join(lines[1:end]))
    if set(metadata) != {"topic", "title", "summary", "aliases"}:
        raise ValueError("Invalid explainer metadata keys")
    topic, title, summary, aliases = (
        metadata[key] for key in ("topic", "title", "summary", "aliases")
    )
    if not isinstance(topic, str) or not _TOPIC.fullmatch(topic):
        raise ValueError("Invalid explainer topic")
    for value, maximum in ((title, 100), (summary, 160)):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError("Invalid explainer title or summary")
    if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
        raise ValueError("Invalid explainer aliases")
    normalized = tuple(alias.strip().casefold() for alias in aliases)
    if any(not _TOPIC.fullmatch(alias) for alias in normalized):
        raise ValueError("Invalid explainer alias")
    if len(set((topic, *normalized))) != len(normalized) + 1:
        raise ValueError("Duplicate explainer alias")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    fence = ""
    for line in lines[end + 1 :]:
        if match := _FENCE.match(line):
            marker = match.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
        elif not fence and line.startswith("## "):
            heading = line[3:].strip()
            if heading not in SECTION_NAMES or heading.lower() in sections:
                raise ValueError("Invalid or duplicate explainer section")
            current = heading.lower()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
        elif line.strip():
            raise ValueError("Content must belong to an explainer section")
    if fence or tuple(sections) != SECTION_IDS:
        raise ValueError("Missing, unordered or unclosed explainer sections")
    contents = tuple((key, "".join(value).strip()) for key, value in sections.items())
    if any(not value or utf16_length(value) > 2500 for _, value in contents):
        raise ValueError("Empty or oversized explainer section")
    return ExplainerDocument(
        ExplainerSpec(topic, title, summary, normalized, package, owner, tools),
        contents,
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def source_path(package: str) -> str | None:
    """Source paths for built-in resources; unknown third-party packages get a digest only."""
    if package == "chatcore":
        return "packages/core/src/chatcore/EXPLAIN.md"
    if package in {"chatcore_" + name for name in BUILTIN_PLUGINS}:
        return f"packages/plugins/{package.removeprefix('chatcore_')}/src/{package}/EXPLAIN.md"
    return None


def _load(package: str, owner: str, tools: tuple[str, ...]) -> ExplainerDocument:
    try:
        with resources.files(package).joinpath("EXPLAIN.md").open("rb") as stream:
            content = stream.read(MAX_DOCUMENT_BYTES + 1)
        return parse_explainer(content.decode("utf-8"), package=package, owner=owner, tools=tools)
    except (ImportError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid explainer for plugin {owner}") from exc


class ExplainerCatalog:
    def __init__(
        self, documents: Sequence[ExplainerDocument], *, source: ExplanationSource | None = None
    ) -> None:
        self.source = source
        self._documents = tuple(sorted(documents, key=lambda document: document.spec.topic))
        self._aliases: dict[str, ExplainerDocument] = {}
        for document in self._documents:
            for name in (document.spec.topic, *document.spec.aliases):
                if name in self._aliases:
                    raise ValueError(f"Duplicate explainer topic/alias from {document.spec.owner}")
                self._aliases[name] = document

    @classmethod
    def from_plugins(
        cls, plugins: Sequence[Plugin], *, source: ExplanationSource | None = None
    ) -> ExplainerCatalog:
        documents = [_load("chatcore", "framework", ())]
        for plugin in plugins:
            package = getattr(plugin, "explainer_package", None)
            if package is not None:
                documents.append(_load(package, plugin.name, tuple(t.name for t in plugin.tools())))
        return cls(documents, source=source)

    def resolve(self, topic: str) -> ExplainerDocument | None:
        return self._aliases.get(topic.strip().casefold())

    def documents(self) -> tuple[ExplainerDocument, ...]:
        return self._documents

    def section(self, topic: str, section: str) -> str:
        document = self.resolve(topic)
        if document is None:
            raise ValueError("Unknown explanation topic")
        try:
            return dict(document.sections)[section.strip().casefold()]
        except KeyError:
            raise ValueError("Unknown explanation section") from None

    def identity(self, document: ExplainerDocument) -> str:
        digest = f"Bundled documentation; sha256:{document.sha256}"
        path = source_path(document.spec.package)
        if self.source is None or path is None:
            return digest
        return (
            f"Source: https://github.com/{self.source.repository}/blob/"
            f"{self.source.revision}/{path}\n{digest}"
        )
