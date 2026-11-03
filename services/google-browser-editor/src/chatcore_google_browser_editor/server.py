"""Private authenticated HTTP protocol for the Google browser worker."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
from datetime import UTC

from aiohttp import web
from pydantic import ValidationError

from .journal import JobRecord
from .models import CreateJobRequest, VerificationRequest
from .worker import WorkerFault, WorkerRuntime

MAX_REQUEST_BYTES = 16 * 1024
_JOB_PATH_ID = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
RUNTIME_KEY = web.AppKey("worker_runtime", WorkerRuntime)
SECRET_KEY = web.AppKey("worker_secret", bytes)
MAINTENANCE_KEY = web.AppKey("maintenance_task", asyncio.Task)


def _record_json(record: JobRecord) -> dict[str, str | None]:
    return {
        "job_id": record.job_id,
        "phase": record.phase,
        "destination_file_id": record.destination_file_id,
        "created_at": record.created_at.astimezone(UTC).isoformat(),
        "updated_at": record.updated_at.astimezone(UTC).isoformat(),
        "error_category": record.error_category,
    }


def create_app(runtime: WorkerRuntime, *, bearer_secret: str) -> web.Application:
    if len(bearer_secret) < 32:
        raise ValueError("worker bearer secret must contain at least 32 characters")
    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    app[RUNTIME_KEY] = runtime
    app[SECRET_KEY] = bearer_secret.encode("utf-8")

    def authorized(request: web.Request) -> bool:
        supplied = request.headers.get("Authorization", "")
        scheme, separator, token = supplied.partition(" ")
        return (
            separator == " "
            and scheme.casefold() == "bearer"
            and bool(token)
            and hmac.compare_digest(token.encode("utf-8"), app[SECRET_KEY])
        )

    def unauthorized() -> web.Response:
        return web.json_response({"error": "unauthorized"}, status=401)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def create_job(request: web.Request) -> web.Response:
        if not authorized(request):
            return unauthorized()
        try:
            body = await request.read()
            if len(body) > MAX_REQUEST_BYTES:
                return web.json_response({"error": "request_too_large"}, status=413)
            submission = CreateJobRequest.model_validate_json(body)
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "request_too_large"}, status=413)
        except (ValidationError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return web.json_response({"error": "invalid_request"}, status=400)
        try:
            kind, record = await runtime.submit(submission)
        except WorkerFault as exc:
            status = 503 if exc.category == "worker_stopping" else 400
            return web.json_response({"error": exc.category}, status=status)
        if kind == "accepted" and record:
            return web.json_response(_record_json(record), status=202)
        if kind == "replay" and record:
            return web.json_response(_record_json(record), status=200)
        if kind == "expired":
            return web.json_response({"error": "expired"}, status=400)
        if kind == "busy":
            return web.json_response({"error": "busy"}, status=429)
        if kind in {"conflict", "tombstoned"}:
            return web.json_response({"error": "id_conflict"}, status=409)
        return web.json_response({"error": "invalid_request"}, status=400)

    def path_job_id(request: web.Request) -> str | None:
        job_id = request.match_info.get("job_id", "")
        return job_id if _JOB_PATH_ID.fullmatch(job_id) else None

    async def get_job(request: web.Request) -> web.Response:
        if not authorized(request):
            return unauthorized()
        job_id = path_job_id(request)
        if not job_id:
            return web.json_response({"error": "not_found"}, status=404)
        record = runtime.journal.get(job_id)
        if not record:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(_record_json(record))

    async def cancel_job(request: web.Request) -> web.Response:
        if not authorized(request):
            return unauthorized()
        job_id = path_job_id(request)
        if not job_id:
            return web.json_response({"error": "not_found"}, status=404)
        record = await runtime.cancel(job_id)
        if not record:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(_record_json(record))

    async def verify_job(request: web.Request) -> web.Response:
        if not authorized(request):
            return unauthorized()
        job_id = path_job_id(request)
        if not job_id:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            body = await request.read()
            ack = VerificationRequest.model_validate_json(body)
        except (web.HTTPRequestEntityTooLarge, ValidationError, json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid_request"}, status=400)
        record = runtime.acknowledge_verification(job_id, ack.result)
        if not record:
            return web.json_response({"error": "not_awaiting_verification"}, status=409)
        return web.json_response(_record_json(record))

    async def delete_job(request: web.Request) -> web.Response:
        if not authorized(request):
            return unauthorized()
        job_id = path_job_id(request)
        if not job_id:
            return web.json_response({"error": "not_found"}, status=404)
        record = runtime.journal.get(job_id)
        if record is None:
            return web.json_response({"error": "not_found"}, status=404)
        if record.phase not in {
            "verified",
            "needs_login",
            "blocked",
            "failed_before_edit",
            "uncertain",
        }:
            await runtime.cancel(job_id)
        if not runtime.delete_metadata(job_id):
            return web.json_response({"error": "job_not_terminal"}, status=409)
        return web.json_response({"job_id": job_id, "deleted": True})

    async def maintenance() -> None:
        while True:
            await asyncio.sleep(60 * 60)
            runtime.journal.purge_expired()

    async def on_startup(_app: web.Application) -> None:
        runtime.journal.recover_after_restart()
        runtime.journal.purge_expired()
        app[MAINTENANCE_KEY] = asyncio.create_task(
            maintenance(), name="browser-journal-maintenance"
        )

    async def on_cleanup(_app: web.Application) -> None:
        task = app.get(MAINTENANCE_KEY)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await runtime.close()

    app.router.add_get("/healthz", health)
    app.router.add_post("/jobs", create_job)
    app.router.add_get("/jobs/{job_id}", get_job)
    app.router.add_post("/jobs/{job_id}/cancel", cancel_job)
    app.router.add_post("/jobs/{job_id}/verification", verify_job)
    app.router.add_delete("/jobs/{job_id}", delete_job)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
