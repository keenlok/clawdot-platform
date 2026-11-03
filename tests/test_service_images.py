"""Framework service image guarantees remain in platform CI after app extraction."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_browser_renderer_dockerfile_runs_venv_python() -> None:
    dockerfile = (ROOT / "services" / "browser-renderer" / "Dockerfile").read_text()

    assert "ENV " in dockerfile
    assert "PATH=/app/.venv/bin:$PATH" in dockerfile
    assert "--package chatcore-browser-renderer" in dockerfile
    assert 'CMD ["python", "-m", "chatcore_browser_renderer"]' in dockerfile
    assert "USER pwuser" in dockerfile


def test_browser_image_contains_its_locked_platform_workspace() -> None:
    dockerfile = (ROOT / "services/browser-renderer/Dockerfile").read_text()
    for source in ("packages", "services", "apps"):
        assert f"COPY {source}/ {source}/" in dockerfile
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "ruff check packages services apps tests" in ci


def test_google_browser_editor_uses_shared_sandboxed_browser_image() -> None:
    dockerfile = (ROOT / "services/google-browser-editor/Dockerfile").read_text()
    renderer = (ROOT / "services/browser-renderer/Dockerfile").read_text()
    pinned_base = next(line for line in renderer.splitlines() if line.startswith("FROM "))
    assert pinned_base in dockerfile
    assert "USER pwuser" in dockerfile
    assert "--package chatcore-google-browser-editor" in dockerfile
    assert 'CMD ["python", "-m", "chatcore_google_browser_editor"]' in dockerfile
    assert "EXPOSE " not in dockerfile
    assert "--no-sandbox" not in dockerfile
