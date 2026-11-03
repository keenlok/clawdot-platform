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
