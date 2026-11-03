"""Canonical documentation folders and local Markdown links stay consistent."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "plans" / "README.md"
_LINK = re.compile(r"\]\(\s*(?:<([^>]+)>|([^\s)]+))")
_REFERENCE = re.compile(r"^\s{0,3}\[[^]]+\]:\s*(?:<([^>]+)>|(\S+))")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def _markdown_targets(path: Path) -> Iterator[str]:
    fence = ""
    for line in path.read_text("utf-8").splitlines():
        if match := _FENCE.match(line):
            marker = match.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
            continue
        if fence:
            continue
        line = re.sub(r"(`+).*?\1", "", line)
        for match in [*_LINK.finditer(line), *_REFERENCE.finditer(line)]:
            yield match.group(1) or match.group(2)


def _local_links(path: Path) -> set[Path]:
    links = set()
    for target in _markdown_targets(path):
        parts = urlsplit(target)
        if parts.scheme or parts.netloc or not parts.path:
            continue
        decoded = unquote(parts.path)
        base = ROOT if decoded.startswith("/") else path.parent
        links.add((base / decoded.lstrip("/")).resolve())
    return links


def _broken_links(paths: Iterable[Path]) -> list[str]:
    return sorted(
        f"{path}: {target}"
        for path in paths
        for target in _local_links(path)
        if not target.exists()
    )


def test_every_plan_is_indexed() -> None:
    records = [
        *(ROOT / "docs" / "plans").rglob("*.md"),
        *(ROOT / "docs" / "superpowers" / "specs").glob("*.md"),
        *(ROOT / "docs" / "handovers").rglob("*.md"),
    ]
    linked = _local_links(INDEX)
    missing = sorted(
        str(path.relative_to(ROOT))
        for path in records
        if path != INDEX and path.resolve() not in linked
    )
    assert not missing, "Add these to docs/plans/README.md:\n" + "\n".join(missing)


def _tracked_markdown() -> list[Path]:
    # Git's file list excludes environments/caches while including newly moved records.
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    paths = [ROOT / name for name in tracked.split("\0") if name]
    assert paths, "The documentation link check must not silently match no files"
    return paths


def test_all_local_markdown_links_resolve() -> None:
    assert not (broken := _broken_links(_tracked_markdown())), "\n".join(broken)


def test_plans_and_handovers_have_one_documentation_home() -> None:
    assert not (ROOT / "plans").exists()
    assert not (ROOT / "handovers").exists()
    assert not (ROOT / "docs" / "superpowers" / "plans").exists()
    assert (ROOT / "docs" / "plans" / "README.md").is_file()
    assert list((ROOT / "docs" / "handovers").rglob("*.md"))
    for directory in ("plans", "handovers"):
        homes = {
            ROOT.joinpath(*parts[: parts.index(directory) + 1])
            for path in _tracked_markdown()
            if directory in (parts := path.relative_to(ROOT).parts)
        }
        assert homes == {ROOT / "docs" / directory}, f"Competing {directory} folders: {homes}"


def test_local_link_validation_reaches_documents_outside_the_plan_index(tmp_path) -> None:
    guide = tmp_path / "README.md"
    guide.write_text("[Missing guide](docs/missing.md)\n")
    assert _broken_links([guide]) == [f"{guide}: {tmp_path / 'docs/missing.md'}"]


def test_local_link_resolution_uses_each_document_and_ignores_examples(tmp_path) -> None:
    guide = tmp_path / "docs" / "guide.md"
    guide.parent.mkdir()
    (tmp_path / "ARCHITECTURE.md").touch()
    (tmp_path / "Design notes.md").touch()
    guide.write_text(
        "[Architecture](../ARCHITECTURE.md#3-architecture)\n"
        "[Design][notes]\n[notes]: <../Design notes.md#example>\n"
        "[Private archive](https://example.com/archive/plans/original.md)\n"
        "[Contact](mailto:maintainers@example.invalid)\n"
        "`[Inline example](not-a-file.md)`\n"
        "```markdown\n[Code example](also-not-a-file.md)\n```\n"
    )
    assert _local_links(guide) == {
        (tmp_path / "ARCHITECTURE.md").resolve(),
        (tmp_path / "Design notes.md").resolve(),
    }
    assert _broken_links([guide]) == []
