import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chatcore_google_browser_editor.journal import JobJournal
from chatcore_google_browser_editor.models import CreateJobRequest
from chatcore_google_browser_editor.server import create_app
from chatcore_google_browser_editor.worker import WorkerRuntime

TEST_BEARER = "test-only-" + "x" * 32


def payload(**changes):
    value = {
        "job_id": "a1b2c3d4e5f6478899aabbccddeeff00",
        "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        "editor_email": "editor@example.com",
        "destination_file_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "destination_tab_id": "t.0",
        "expected_revision": "revision-opaque-01",
        "operation": {"kind": "replace_text", "find": "before", "replacement": "after"},
    }
    value.update(changes)
    return value


def job_id(number: int) -> str:
    return f"a1b2c3d4e5f6478899aabbccddeeff{number:02d}"


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, request, operation, cancel_event, mark_running):
        self.calls.append(request.job_id)
        self.started.set()
        await self.release.wait()
        if cancel_event.is_set():
            return "failed_before_edit"
        await mark_running()
        return "awaiting_verification"


@pytest.fixture
async def client(tmp_path):
    journal = JobJournal(tmp_path / "jobs.sqlite3")
    engine = FakeEngine()
    runtime = WorkerRuntime(journal, engine, editor_email="editor@example.com")
    app = create_app(runtime, bearer_secret=TEST_BEARER)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client, runtime, engine, journal
    await test_client.close()


def headers():
    return {"Authorization": f"Bearer {TEST_BEARER}"}


async def test_health_is_unauthenticated_and_contains_only_health_status(client) -> None:
    test_client, *_ = client
    response = await test_client.get("/healthz")
    assert response.status == 200
    assert await response.json() == {"status": "ok"}


async def test_job_endpoints_require_bearer_auth(client) -> None:
    test_client, *_ = client
    response = await test_client.get("/jobs/a1b2c3d4e5f6478899aabbccddeeff00")
    assert response.status == 401
    response = await test_client.post("/jobs", json=payload())
    assert response.status == 401


async def test_same_payload_replay_never_runs_engine_twice(client) -> None:
    test_client, _runtime, engine, _journal = client
    body = payload()
    first = await test_client.post("/jobs", json=body, headers=headers())
    assert first.status == 202
    await engine.started.wait()
    replay = await test_client.post("/jobs", json=body, headers=headers())
    assert replay.status == 200
    assert (await replay.json())["job_id"] == body["job_id"]
    different = payload(
        operation={"kind": "replace_text", "find": "before", "replacement": "other"}
    )
    conflict = await test_client.post("/jobs", json=different, headers=headers())
    assert conflict.status == 409
    assert engine.calls == [body["job_id"]]
    engine.release.set()


async def test_unknown_fields_and_invalid_or_expired_deadlines_do_not_dispatch(client) -> None:
    test_client, _runtime, engine, _journal = client
    unknown = payload(arbitrary_url="https://attacker.invalid")
    response = await test_client.post("/jobs", json=unknown, headers=headers())
    assert response.status == 400
    expired = payload(expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    response = await test_client.post("/jobs", json=expired, headers=headers())
    assert response.status == 400
    too_late = payload(expires_at=(datetime.now(UTC) + timedelta(seconds=91)).isoformat())
    response = await test_client.post("/jobs", json=too_late, headers=headers())
    assert response.status == 400
    assert engine.calls == []


async def test_oversized_request_is_rejected(client) -> None:
    test_client, *_ = client
    body = payload(extra="x" * 18_000)
    response = await test_client.post(
        "/jobs", data=json.dumps(body), headers={**headers(), "Content-Type": "application/json"}
    )
    assert response.status == 413


async def test_status_and_verification_are_bounded_and_verification_needs_awaiting_state(
    client,
) -> None:
    test_client, _runtime, engine, journal = client
    body = payload()
    await test_client.post("/jobs", json=body, headers=headers())
    await engine.started.wait()
    waiting = await test_client.get(f"/jobs/{body['job_id']}", headers=headers())
    assert (await waiting.json())["phase"] == "accepted"
    premature = await test_client.post(
        f"/jobs/{body['job_id']}/verification", json={"result": "verified"}, headers=headers()
    )
    assert premature.status == 409
    engine.release.set()
    for _ in range(50):
        record = journal.get(body["job_id"])
        if record and record.phase == "awaiting_verification":
            break
        await asyncio.sleep(0.01)
    assert record and record.phase == "awaiting_verification", record
    verified = await test_client.post(
        f"/jobs/{body['job_id']}/verification", json={"result": "verified"}, headers=headers()
    )
    assert verified.status == 200
    assert (await verified.json())["phase"] == "verified"
    final = await test_client.get(f"/jobs/{body['job_id']}", headers=headers())
    result = await final.json()
    assert set(result) == {
        "job_id",
        "phase",
        "destination_file_id",
        "created_at",
        "updated_at",
        "error_category",
    }


async def test_cancel_before_first_mutation_and_delete_keep_replay_tombstone(client) -> None:
    test_client, _runtime, engine, journal = client
    body = payload()
    await test_client.post("/jobs", json=body, headers=headers())
    await engine.started.wait()
    response = await test_client.post(f"/jobs/{body['job_id']}/cancel", headers=headers())
    assert response.status == 200
    engine.release.set()
    for _ in range(50):
        record = journal.get(body["job_id"])
        if record and record.phase == "failed_before_edit":
            break
        await asyncio.sleep(0.01)
    deleted = await test_client.delete(f"/jobs/{body['job_id']}", headers=headers())
    assert deleted.status == 200
    assert journal.get(body["job_id"]) is None
    replay = await test_client.post("/jobs", json=body, headers=headers())
    assert replay.status == 409


async def test_cancelled_queue_entries_release_memory_and_capacity(client) -> None:
    test_client, runtime, engine, _journal = client
    first = payload(job_id=job_id(1))
    second = payload(job_id=job_id(2))
    third = payload(job_id=job_id(3))
    fourth = payload(job_id=job_id(4))
    assert (await test_client.post("/jobs", json=first, headers=headers())).status == 202
    await engine.started.wait()
    assert (await test_client.post("/jobs", json=second, headers=headers())).status == 202
    assert (
        await test_client.post(f"/jobs/{second['job_id']}/cancel", headers=headers())
    ).status == 200
    assert second["job_id"] not in runtime._operations
    assert (await test_client.post("/jobs", json=third, headers=headers())).status == 202
    assert (
        await test_client.post(f"/jobs/{third['job_id']}/cancel", headers=headers())
    ).status == 200
    assert third["job_id"] not in runtime._operations
    accepted = await test_client.post("/jobs", json=fourth, headers=headers())
    assert accepted.status == 202
    engine.release.set()
    for _ in range(50):
        record = _journal.get(fourth["job_id"])
        if record and record.phase == "awaiting_verification":
            break
        await asyncio.sleep(0.01)
    assert engine.calls == [first["job_id"], fourth["job_id"]]


async def test_running_cancel_classifies_possible_autosave_as_uncertain(tmp_path) -> None:
    class RunningEngine:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, request, operation, cancel_event, before_mutation):
            await before_mutation()
            self.started.set()
            await self.release.wait()
            return "awaiting_verification"

    journal = JobJournal(tmp_path / "jobs.sqlite3")
    engine = RunningEngine()
    runtime = WorkerRuntime(journal, engine, editor_email="editor@example.com")
    request = CreateJobRequest.model_validate_json(json.dumps(payload()))
    await runtime.submit(request)
    await engine.started.wait()
    assert journal.get(request.job_id).phase == "running"
    assert (await runtime.cancel(request.job_id)).phase == "running"
    engine.release.set()
    for _ in range(50):
        record = journal.get(request.job_id)
        if record and record.phase == "uncertain":
            break
        await asyncio.sleep(0.01)
    assert record and record.phase == "uncertain"
    await runtime.close()


async def test_only_one_queued_job_is_admitted(client) -> None:
    test_client, _runtime, engine, _journal = client
    first, second, third = (payload(job_id=job_id(index)) for index in (1, 2, 3))
    assert (await test_client.post("/jobs", json=first, headers=headers())).status == 202
    await engine.started.wait()
    assert (await test_client.post("/jobs", json=second, headers=headers())).status == 202
    busy = await test_client.post("/jobs", json=third, headers=headers())
    assert busy.status == 429


async def test_wrong_editor_account_is_refused_before_reservation(client) -> None:
    test_client, _runtime, engine, journal = client
    response = await test_client.post(
        "/jobs", json=payload(editor_email="owner@example.com"), headers=headers()
    )
    assert response.status == 400
    assert journal.get("a1b2c3d4e5f6478899aabbccddeeff00") is None
    assert engine.calls == []
