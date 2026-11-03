"""Every shipped capability owns bounded documentation in its installed package."""

import ast
from importlib import resources
from pathlib import Path

import pytest
from chatcore.explainers import ExplainerCatalog, parse_explainer

PACKAGES = (
    "browse",
    "expenses",
    "extensions",
    "google",
    "jobs",
    "memory",
    "polls",
    "research",
    "search",
    "stocks",
    "tutor",
    "web",
)
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", PACKAGES)
def test_plugin_declares_its_own_packaged_explainer(name):
    package = "chatcore_" + name
    resource = resources.files(package).joinpath("EXPLAIN.md")
    assert resource.is_file(), f"Missing packaged explainer for {name}"
    document = parse_explainer(resource.read_text("utf-8"), package=package, owner=name, tools=())
    assert document.spec.topic == name
    plugin_source = ROOT / "packages" / "plugins" / name / "src" / package / "plugin.py"
    declarations = [
        ast.literal_eval(node.value)
        for node in ast.walk(ast.parse(plugin_source.read_text()))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "explainer_package"
            for target in node.targets
        )
    ]
    assert declarations == [package]


def test_all_builtin_topic_aliases_are_unique():
    documents = list(ExplainerCatalog.from_plugins([]).documents())
    for name in PACKAGES:
        package = "chatcore_" + name
        documents.append(
            parse_explainer(
                resources.files(package).joinpath("EXPLAIN.md").read_text("utf-8"),
                package=package,
                owner=name,
                tools=(),
            )
        )
    catalog = ExplainerCatalog(documents)
    assert len(catalog.documents()) == 13
    assert catalog.resolve("trips").spec.topic == "expenses"
    assert catalog.resolve("google_docs").spec.topic == "google"
