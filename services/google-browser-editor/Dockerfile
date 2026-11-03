# Keep the renderer and editor on one reviewed Chromium build.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble@sha256:330d401ae23c905896a0c26c66c36fc971b621519e7b11b435a8328d7bf6e031

COPY --from=ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp PATH=/app/.venv/bin:$PATH
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY services/ services/
COPY apps/ apps/
RUN uv sync --frozen --no-dev --package chatcore-google-browser-editor

USER pwuser
CMD ["python", "-m", "chatcore_google_browser_editor"]
