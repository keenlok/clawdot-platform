"""Packaged explanatory evidence has a bounded, validated document contract."""

import hashlib
from dataclasses import replace

import pytest


def document(*, topic="feature", aliases='["alias"]', body="Stored records."):
    return (
        f'+++\ntopic = "{topic}"\ntitle = "A feature"\nsummary = "What it does"\n'
        f"aliases = {aliases}\n+++\n\n"
        + "\n\n".join(
            f"## {name}\n{body}"
            for name in ("Overview", "Workflow", "Storage", "Safety", "Limitations", "Verification")
        )
        + "\n"
    )


def parse(text):
    from chatcore.explainers import parse_explainer

    return parse_explainer(text, package="feature", owner="feature", tools=("do_feature",))


def test_alias_resolves_packaged_document_without_a_checkout():
    from chatcore.explainers import ExplainerCatalog

    catalog = ExplainerCatalog.from_plugins([])
    assert catalog.resolve("architecture").spec.topic == "framework"
    assert "Postgres" in catalog.section("framework", "storage")
    assert catalog.resolve("../../.env") is None
    assert catalog.resolve("https://example.com") is None


def test_exact_sections_and_digest_preserve_source():
    text = document(body="First paragraph.\n\nSecond paragraph.")
    result = parse(text)
    assert dict(result.sections)["storage"] == "First paragraph.\n\nSecond paragraph."
    assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert result.spec.tools == ("do_feature",)


@pytest.mark.parametrize(
    "text",
    [
        document(topic="../bad"),
        document(aliases='["alias", " ALIAS "]'),
        document(aliases='["../bad"]'),
        document().replace('title = "A feature"', 'title = ""'),
        document().replace('title = "A feature"', 'title = "' + "x" * 101 + '"'),
        document().replace('summary = "What it does"', 'summary = "' + "x" * 161 + '"'),
        document().replace("## Storage", "## Unknown"),
        document() + "\n## Storage\nDuplicate.",
        document().replace("## Workflow\nStored records.", ""),
        document().replace("+++", "---", 1),
        document().replace('topic = "feature"', 'topic = "feature"\ntopic = "feature"'),
        document(aliases="[1]"),
        document(body="😀" * 1251),
        document() + "x" * (24 * 1024),
    ],
)
def test_invalid_documents_fail_closed(text):
    with pytest.raises(ValueError):
        parse(text)


def test_fenced_example_headings_do_not_create_sections():
    result = parse(document(body="Before.\n```markdown\n## Fake\nExample\n```\nAfter."))
    assert "## Fake" in dict(result.sections)["storage"]
    assert len(result.sections) == 6


def test_legacy_plugin_without_resource_is_compatible():
    from chatcore.explainers import ExplainerCatalog
    from chatcore.plugin import BasePlugin

    class Legacy(BasePlugin):
        name = "legacy"

    assert len(ExplainerCatalog.from_plugins([Legacy()]).documents()) == 1


def test_duplicate_topics_and_missing_resources_fail_with_owner():
    from chatcore.explainers import ExplainerCatalog
    from chatcore.plugin import BasePlugin

    class Invalid(BasePlugin):
        name = "invalid"
        explainer_package = "chatcore.testing"

    with pytest.raises(ValueError, match="invalid"):
        ExplainerCatalog.from_plugins([Invalid()])

    class Duplicate(BasePlugin):
        name = "duplicate"
        explainer_package = "chatcore"

    with pytest.raises(ValueError, match="duplicate"):
        ExplainerCatalog.from_plugins([Duplicate()])


@pytest.mark.parametrize(
    ("repository", "revision"),
    [
        ("../private", "v0.2.0"),
        ("a/b", "main"),
        ("a/b", "../secret"),
        ("a/b", "a" * 39),
        ("owner/" + "r" * 101, "a" * 40),
        ("a/b", "v" + "1" * 100 + ".2.0"),
    ],
)
def test_invalid_source_identity_rejected(repository, revision):
    from chatcore.explainers import ExplanationSource

    with pytest.raises(ValueError):
        ExplanationSource(repository, revision)


def test_source_identity_uses_reviewed_revision_and_digest():
    from chatcore.explainers import ExplainerCatalog, ExplanationSource

    source = ExplanationSource("owner/project", "a" * 40)
    catalog = ExplainerCatalog.from_plugins([], source=source)
    identity = catalog.identity(catalog.resolve("framework"))
    assert "owner/project/blob/" + "a" * 40 + "/packages/core/src/chatcore/EXPLAIN.md" in identity
    assert "sha256:" in identity
    assert "/main/" not in identity


def test_unknown_section_is_not_a_path():
    from chatcore.explainers import ExplainerCatalog

    catalog = ExplainerCatalog.from_plugins([])
    with pytest.raises(ValueError, match="section"):
        catalog.section("framework", "../.env")


def test_vendor_package_does_not_receive_false_platform_source_attribution():
    from chatcore.explainers import ExplainerCatalog, ExplanationSource

    parsed = parse(document())
    vendor = replace(parsed, spec=replace(parsed.spec, package="chatcore_vendor"))
    catalog = ExplainerCatalog([vendor], source=ExplanationSource("owner/project", "a" * 40))
    assert "github.com" not in catalog.identity(vendor)
