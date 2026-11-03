"""Generate copy-ready wiki pages from canonical packaged explainers; never publish them."""

from __future__ import annotations

import argparse
import posixpath
import re
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from chatcore.explainers import BUILTIN_PLUGINS, ExplainerCatalog, parse_explainer, source_path

MARKER = "<!-- Generated from packaged EXPLAIN.md; do not edit. -->"
REPOSITORY = "https://github.com/keenlok/clawdot-platform"
_LINK = re.compile(r"(!?\[[^\]\n]*\]\()([^\s)]+)(\))")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def rewrite_links(text: str, source: str, ref: str) -> str:
    def replace(match: re.Match[str]) -> str:
        target = urlsplit(match[2])
        if target.scheme or target.netloc or not target.path:
            return match[0]
        path = posixpath.normpath(
            posixpath.join(posixpath.dirname(source), target.path)
            if not target.path.startswith("/")
            else target.path.lstrip("/")
        )
        if path == ".." or path.startswith("../"):
            raise ValueError("Wiki link would escape repository")
        url = urlunsplit(
            (
                "https",
                "github.com",
                f"/keenlok/clawdot-platform/blob/{ref}/{quote(path)}",
                target.query,
                target.fragment,
            )
        )
        return match[1] + url + match[3]

    lines = []
    fence = ""
    for line in text.splitlines(keepends=True):
        if match := _FENCE.match(line):
            marker = match[1]
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
            lines.append(line)
        else:
            if fence:
                lines.append(line)
                continue
            cursor = 0
            for code in re.finditer(r"(`+)(.*?)\1", line):
                lines.extend((_LINK.sub(replace, line[cursor : code.start()]), code[0]))
                cursor = code.end()
            lines.append(_LINK.sub(replace, line[cursor:]))
    return "".join(lines)


def generate_pages(root: Path, ref: str) -> dict[str, str]:
    if len(ref) > 64 or not re.fullmatch(r"(?:main|v[0-9]+\.[0-9]+\.[0-9]+|[a-f0-9]{40})", ref):
        raise ValueError("Invalid source revision")
    documents = []
    for name in ("framework", *BUILTIN_PLUGINS):
        package = "chatcore" if name == "framework" else "chatcore_" + name
        path = source_path(package)
        assert path is not None
        documents.append(
            parse_explainer(
                (root / path).read_text("utf-8"),
                package=package,
                owner=name,
                tools=(),
            )
        )
    catalog = ExplainerCatalog(documents)
    pages = {}
    index = [MARKER, "# Explainers", "", "Generated from maintained plugin documentation.", ""]
    for document in catalog.documents():
        name = (
            "How-the-bot-works"
            if document.spec.topic == "framework"
            else "Plugin-" + document.spec.topic
        )
        filename = name + ".md"
        if filename in pages:
            raise ValueError("Generated wiki filename collision")
        path = source_path(document.spec.package)
        assert path is not None
        lines = [
            MARKER,
            f"# {document.spec.title}",
            "",
            f"[Canonical source]({REPOSITORY}/blob/{ref}/{path}) · sha256:{document.sha256}",
            "",
            "This page describes the selected platform source. "
            "Your app's enabled features may differ.",
            "",
        ]
        for section, body in document.sections:
            lines.extend([f"## {section.capitalize()}", rewrite_links(body, path, ref), ""])
        pages[filename] = "\n".join(lines)
        index.append(
            f"- [{document.spec.title}]({REPOSITORY}/wiki/{name}) — {document.spec.summary}"
        )
    pages["Explainers.md"] = "\n".join(index) + "\n"
    return pages


def write_pages(pages: dict[str, str], directory: Path, *, check: bool) -> bool:
    paths = {
        *directory.glob("Plugin-*.md"),
        *(
            directory / name
            for name in ("How-the-bot-works.md", "Explainers.md")
            if (directory / name).exists()
        ),
    }
    for path in paths:
        if not path.read_text("utf-8").startswith(MARKER):
            raise ValueError(f"Refusing to overwrite unmanaged wiki page: {path.name}")
    stale = [path for path in paths if path.name not in pages]
    changed = [
        name
        for name, text in pages.items()
        if not (directory / name).is_file() or (directory / name).read_text("utf-8") != text
    ]
    if check:
        return not stale and not changed
    directory.mkdir(parents=True, exist_ok=True)
    for name in changed:
        (directory / name).write_text(pages[name], encoding="utf-8")
    for path in stale:
        path.unlink()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--source-ref", default="main")
    root = Path(__file__).resolve().parents[2]
    parser.add_argument("--output-dir", type=Path, default=root / "docs/wiki")
    arguments = parser.parse_args()
    if not write_pages(
        generate_pages(root, arguments.source_ref), arguments.output_dir, check=arguments.check
    ):
        parser.exit(1, "Generated wiki pages are stale; regenerate from packaged explainers.\n")


if __name__ == "__main__":
    main()
