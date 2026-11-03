"""plans/README.md indexes every plan and design record (AGENTS.md "Plans and status")."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "plans" / "README.md"
_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def _local_links() -> set[Path]:
    links = set()
    for target in _LINK.findall(INDEX.read_text("utf-8")):
        if "://" not in target:
            links.add((INDEX.parent / unquote(target)).resolve())
    return links


def test_every_plan_is_indexed() -> None:
    records = [
        *(ROOT / "plans").glob("*.md"),
        *(ROOT / "docs" / "superpowers" / "plans").glob("*.md"),
        *(ROOT / "docs" / "superpowers" / "specs").glob("*.md"),
        *(ROOT / "handovers").glob("*.md"),
    ]
    linked = _local_links()
    missing = sorted(
        str(path.relative_to(ROOT))
        for path in records
        if path != INDEX and path.resolve() not in linked
    )
    assert not missing, "Add these to plans/README.md:\n" + "\n".join(missing)


def test_index_links_resolve() -> None:
    broken = sorted(str(path) for path in _local_links() if not path.exists())
    assert not broken, "\n".join(broken)
