"""Import boundaries between apps, plugins and framework services (ARCHITECTURE.md §3d)."""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Core (``chatcore``) is the framework API; apps may import its submodules.
_PLUGIN_PREFIX = "chatcore_"


# Every package under packages/ (core, adapters/*, plugins/*); see the workspace members.
_PACKAGE_SOURCES = ("packages/core/src/**/*.py", "packages/*/*/src/**/*.py")


def _sources(*globs: str) -> Iterator[Path]:
    for pattern in globs:
        matches = sorted(ROOT.glob(pattern))
        # A glob that silently matches nothing would turn a boundary check into a no-op.
        assert matches, f"boundary glob matched no files: {pattern}"
        yield from matches


def _imports(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module


def _owner(path: Path) -> str | None:
    """Return the top-level package that ``path`` belongs to, from its ``src/`` layout."""
    parts = path.relative_to(ROOT).parts
    return parts[parts.index("src") + 1] if "src" in parts else None


def test_apps_import_plugins_only_through_their_top_level_package() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{line}: {module}"
        for path in _sources("apps/*/src/**/*.py")
        for line, module in _imports(path)
        if module.startswith(_PLUGIN_PREFIX) and "." in module
    ]
    assert not offenders, (
        "Apps must import plugin and adapter names from the package itself; export the name "
        "from its __init__.py instead:\n" + "\n".join(offenders)
    )


def _application_modules() -> set[str]:
    return {
        path.stem if path.is_file() else path.name
        for path in ROOT.glob("apps/*/src/*")
        if (path.is_file() and path.suffix == ".py") or (path.is_dir() and any(path.rglob("*.py")))
    }


def test_framework_never_imports_apps() -> None:
    # Downstream private apps remain forbidden even after leaving this workspace.
    applications = _application_modules() | {"sb_bot"}
    offenders = [
        f"{path.relative_to(ROOT)}:{line}: {module}"
        for path in _sources(*_PACKAGE_SOURCES, "services/*/src/**/*.py")
        for line, module in _imports(path)
        if module.split(".")[0] in applications
    ]
    assert not offenders, "\n".join(offenders)


def test_plugins_do_not_import_other_plugins_skills_or_gates() -> None:
    # Skills that span plugins are app composites (docs/plans/skill-layer.md §3.4).
    offenders = []
    for path in _sources(*_PACKAGE_SOURCES):
        owner = _owner(path)
        for line, module in _imports(path):
            parts = module.split(".")
            if (
                parts[0].startswith(_PLUGIN_PREFIX)
                and parts[0] != owner
                and len(parts) > 1
                and parts[1] in {"skills", "gates"}
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {module}")
    assert not offenders, "\n".join(offenders)


def _is_reexport_statement(node: ast.stmt) -> bool:
    if isinstance(node, ast.Import | ast.ImportFrom):
        return True
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        return isinstance(node.value.value, str)  # docstring
    if isinstance(node, ast.Assign | ast.AnnAssign):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return all(isinstance(t, ast.Name) and t.id in {"__all__", "_EXPORTS"} for t in targets)
    if isinstance(node, ast.If):  # ``if TYPE_CHECKING:`` import block
        return ast.unparse(node.test) == "TYPE_CHECKING" and all(
            isinstance(child, ast.Import | ast.ImportFrom) for child in node.body
        )
    # PEP 562 lazy exports (chatcore_extensions).
    return isinstance(node, ast.FunctionDef) and node.name in {"__getattr__", "__dir__"}


def test_plugin_init_modules_only_reexport() -> None:
    # AGENTS.md "Plugin module layout": code lives in plugin.py / settings.py / gates.py.
    offenders = [
        f"{path.relative_to(ROOT)}:{node.lineno}: {type(node).__name__}"
        for path in _sources("packages/plugins/*/src/*/__init__.py")
        for node in ast.parse(path.read_text("utf-8")).body
        if not _is_reexport_statement(node)
    ]
    assert not offenders, "\n".join(offenders)


def test_plugin_gates_live_in_gates_module() -> None:
    # AGENTS.md "Plugin module layout": gates are reviewed apart from ordinary tool code.
    offenders = []
    for path in _sources("packages/plugins/*/src/**/*.py"):
        if path.name == "gates.py" or "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"Gate", "ToolGate"}
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.func.id}(...)")
    assert not offenders, "\n".join(offenders)


_PUBLIC_PACKAGES = (
    "chatcore_browse",
    "chatcore_console",
    "chatcore_expenses",
    "chatcore_extensions",
    "chatcore_jobs",
    "chatcore_memory",
    "chatcore_polls",
    "chatcore_research",
    "chatcore_search",
    "chatcore_stocks",
    "chatcore_telegram",
    "chatcore_web",
)


@pytest.mark.parametrize("package", _PUBLIC_PACKAGES)
def test_declared_exports_resolve(package: str) -> None:
    module = importlib.import_module(package)
    for name in getattr(module, "__all__", ()):
        assert getattr(module, name) is not None, f"{package}.{name}"


def test_extension_lazy_exports_match_all() -> None:
    import chatcore_extensions

    assert set(chatcore_extensions.__all__) == set(chatcore_extensions._EXPORTS)


def test_extension_worker_import_stays_minimal() -> None:
    # The worker is a privileged process; the package's lazy exports must not pull the
    # controller, generator or Postgres code into it.
    code = (
        "import sys, chatcore_extensions.worker; "
        "print(','.join(sorted(m for m in sys.modules if m.startswith('chatcore_extensions'))))"
    )
    loaded = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert set(loaded.split(",")) == {
        "chatcore_extensions",
        "chatcore_extensions.models",
        "chatcore_extensions.runtime",
        "chatcore_extensions.sandbox",
        "chatcore_extensions.worker",
    }


def test_framework_boundary_detects_a_new_application_package(tmp_path, monkeypatch) -> None:
    app_module = tmp_path / "apps" / "copied_bot" / "src" / "another_bot" / "__init__.py"
    app_module.parent.mkdir(parents=True)
    app_module.write_text('"""A newly copied application."""\n')
    framework_module = tmp_path / "framework.py"
    framework_module.write_text("from another_bot import app\n")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "_sources", lambda *patterns: iter([framework_module]))
    with pytest.raises(AssertionError, match="another_bot"):
        test_framework_never_imports_apps()


def test_framework_boundary_rejects_private_app_after_extraction(tmp_path, monkeypatch) -> None:
    framework_module = tmp_path / "framework.py"
    framework_module.write_text("from sb_bot import app\n")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "_sources", lambda *patterns: iter([framework_module]))
    with pytest.raises(AssertionError, match="sb_bot"):
        test_framework_never_imports_apps()
