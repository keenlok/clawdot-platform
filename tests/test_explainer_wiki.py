"""Wiki pages reuse packaged source and preserve unrelated publication content."""

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def api():
    return runpy.run_path(str(ROOT / ".github/scripts/generate_explainers.py"))


def test_wiki_generation_is_deterministic_and_contains_exact_source_sections(tmp_path):
    from chatcore.explainers import parse_explainer

    generator = api()
    pages = generator["generate_pages"](ROOT, "v0.2.0")
    assert len(pages) == 14
    source = ROOT / "packages/plugins/expenses/src/chatcore_expenses/EXPLAIN.md"
    doc = parse_explainer(
        source.read_text(), package="chatcore_expenses", owner="expenses", tools=()
    )
    page = pages["Plugin-expenses.md"]
    assert "+++" not in page
    assert "sha256:" + doc.sha256 in page
    assert "/blob/v0.2.0/" in page
    assert dict(doc.sections)["storage"] in page
    assert pages == generator["generate_pages"](ROOT, "v0.2.0")
    (tmp_path / "Home.md").write_text("Keep my navigation.")
    (tmp_path / "Storage-architecture.md").write_text("Keep this guide.")
    assert generator["write_pages"](pages, tmp_path, check=False)
    assert generator["write_pages"](pages, tmp_path, check=True)
    assert (tmp_path / "Home.md").read_text() == "Keep my navigation."
    assert (tmp_path / "Storage-architecture.md").read_text() == "Keep this guide."
    (tmp_path / "Plugin-expenses.md").write_text(page + "drift")
    assert not generator["write_pages"](pages, tmp_path, check=True)
    assert (tmp_path / "Plugin-expenses.md").read_text().endswith("drift")


def test_publication_refuses_to_overwrite_unrelated_page(tmp_path):
    generator = api()
    (tmp_path / "Plugin-expenses.md").write_text("Someone else's page.")
    with pytest.raises(ValueError, match="unmanaged"):
        generator["write_pages"](generator["generate_pages"](ROOT, "main"), tmp_path, check=False)


def test_stale_managed_pages_are_detected_and_removed_only_when_writing(tmp_path):
    generator = api()
    pages = generator["generate_pages"](ROOT, "main")
    stale = tmp_path / "Plugin-removed.md"
    stale.write_text(generator["MARKER"] + "\nOld plugin.")
    assert not generator["write_pages"](pages, tmp_path, check=True)
    assert stale.exists()
    assert generator["write_pages"](pages, tmp_path, check=False)
    assert not stale.exists()


def test_relative_link_rewriting_is_bounded_and_skips_code_examples():
    rewrite = api()["rewrite_links"]
    path = "packages/plugins/expenses/src/chatcore_expenses/EXPLAIN.md"
    text = (
        "[Code](repository.py#ledger)\n[Website](https://example.com)\n```\n[Example](../.env)\n```"
    )
    rewritten = rewrite(text, path, "v0.2.0")
    assert (
        "https://github.com/keenlok/clawdot-platform/blob/v0.2.0/packages/plugins/expenses/src/chatcore_expenses/repository.py#ledger"
        in rewritten
    )
    assert "[Website](https://example.com)" in rewritten
    assert "[Example](../.env)" in rewritten
    assert rewrite("`[Example](repository.py)`", path, "main") == "`[Example](repository.py)`"
    with pytest.raises(ValueError, match="escape"):
        rewrite("[Outside](../../../../../../outside)", path, "main")
    with pytest.raises(ValueError, match="revision"):
        api()["generate_pages"](ROOT, "../private")
