"""Start the private worker HTTP service."""

from __future__ import annotations

from aiohttp import web

from .engine import BrowserEngine
from .journal import JobJournal
from .server import create_app
from .settings import WorkerSettings
from .worker import WorkerRuntime


def main() -> None:
    settings = WorkerSettings()
    secret = settings.read_secret()
    settings.read_storage_state()
    journal = JobJournal(
        settings.db_path,
        terminal_retention=settings.terminal_retention_hours * 60 * 60,
        tombstone_retention=settings.tombstone_retention_days * 24 * 60 * 60,
    )
    engine = BrowserEngine(settings)
    runtime = WorkerRuntime(
        journal,
        engine,
        editor_email=settings.editor_email,
        job_timeout_seconds=settings.job_timeout_seconds,
    )
    app = create_app(runtime, bearer_secret=secret)
    host, port = settings.bind_address()
    web.run_app(app, host=host, port=port, access_log=None)
