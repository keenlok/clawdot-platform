from __future__ import annotations

import pytest
from chatcore.events import ChatRef, UserRef
from chatcore_extensions.models import Artifact, ExampleCase, RunResult
from chatcore_extensions.repository import MemoryExtensionRepo
from chatcore_extensions.service import ExtensionService, ExtensionServiceError


class _Generator:
    def __init__(self, artifact):
        self.artifact = artifact

    async def generate(self, **_kwargs):
        return self.artifact


class _Worker:
    image = "sha256:" + "a" * 64
    policy = "b" * 64
    harness = "c" * 64

    async def health(self):
        return {
            "image_id": self.image,
            "policy_digest": self.policy,
            "harness_digest": self.harness,
        }

    async def run(self, *, artifact, arguments, **_kwargs):
        value = True if "return True" in artifact.source else artifact.cases[0].expected
        if arguments.get("actual") is not None:
            value = arguments["actual"]
        return RunResult("ok", value, 3, self.image, self.policy, self.harness)


@pytest.mark.asyncio
async def test_build_evidence_activation_and_post_revoke_suppression() -> None:
    chat, actor = ChatRef("tg", "-1001"), UserRef("tg", "7")
    artifact = Artifact(
        "identity", "def run(arguments): return arguments", (ExampleCase({"x": 1}, 1),)
    )
    repo, worker = MemoryExtensionRepo(), _Worker()
    service = ExtensionService(repo, _Generator(artifact), worker, enabled=lambda: True)
    request_id = await service.request(
        chat=chat, actor=actor, description="identity", examples=artifact.cases
    )
    revision_id = await service.build(chat=chat, actor=actor, request_id=request_id)
    await service.activate(chat=chat, actor=actor, revision_id=revision_id, digest=artifact.digest)
    result = await service.run(chat=chat, actor=actor, revision_id=revision_id, arguments={"x": 2})
    assert result.status == "ok" and result.value == 1
    await service.revoke(chat=chat, actor=actor, revision_id=revision_id)
    with pytest.raises(ExtensionServiceError):
        await service.run(chat=chat, actor=actor, revision_id=revision_id, arguments={})


@pytest.mark.asyncio
async def test_build_rejects_bool_int_test_equality() -> None:
    chat, actor = ChatRef("tg", "-1001"), UserRef("tg", "7")
    artifact = Artifact("identity", "def run(arguments): return True", (ExampleCase({}, 1),))
    service = ExtensionService(
        MemoryExtensionRepo(), _Generator(artifact), _Worker(), enabled=lambda: True
    )
    req = await service.request(chat=chat, actor=actor, description="x", examples=artifact.cases)
    with pytest.raises(ExtensionServiceError):
        await service.build(chat=chat, actor=actor, request_id=req)


@pytest.mark.asyncio
async def test_revocation_and_kill_switch_suppress_inflight_result() -> None:
    import asyncio

    class GateWorker(_Worker):
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0
            self.block = False

        async def run(self, *, artifact, arguments, **_kwargs):
            self.calls += 1
            if self.block:
                self.started.set()
                await self.release.wait()
            return RunResult("ok", 10, 3, self.image, self.policy, self.harness)

    chat, actor = ChatRef("tg", "-1001"), UserRef("tg", "7")
    artifact = Artifact("x", "def run(arguments): return 10", (ExampleCase({}, 10),))
    repo, worker = MemoryExtensionRepo(), GateWorker()
    enabled = True
    service = ExtensionService(repo, _Generator(artifact), worker, enabled=lambda: enabled)
    request = await service.request(
        chat=chat, actor=actor, description="x", examples=artifact.cases
    )
    revision = await service.build(chat=chat, actor=actor, request_id=request)
    await service.activate(chat=chat, actor=actor, revision_id=revision, digest=artifact.digest)
    worker.block = True
    task = asyncio.create_task(
        service.run(chat=chat, actor=actor, revision_id=revision, arguments={})
    )
    await asyncio.wait_for(worker.started.wait(), timeout=2)
    await service.revoke(chat=chat, actor=actor, revision_id=revision)
    worker.release.set()
    result = await task
    assert result.status == "unavailable" and result.value is None

    # A global kill switch blocks new work before quota reservation.
    enabled = False
    before = len(repo._runs)
    with pytest.raises(ExtensionServiceError):
        await service.run(chat=chat, actor=actor, revision_id=revision, arguments={})
    assert len(repo._runs) == before


@pytest.mark.asyncio
async def test_three_failures_suspend_and_fresh_activation_resets_failures() -> None:
    class StatusWorker(_Worker):
        status = "ok"

        async def run(self, *, artifact, arguments, **_kwargs):
            value = 10 if self.status == "ok" else None
            return RunResult(self.status, value, 1, self.image, self.policy, self.harness)

    chat, actor = ChatRef("tg", "-1001"), UserRef("tg", "7")
    artifact = Artifact("x", "def run(arguments): return 10", (ExampleCase({}, 10),))
    repo, worker = MemoryExtensionRepo(), StatusWorker()
    service = ExtensionService(repo, _Generator(artifact), worker, enabled=lambda: True)
    request = await service.request(
        chat=chat, actor=actor, description="x", examples=artifact.cases
    )
    revision = await service.build(chat=chat, actor=actor, request_id=request)
    await service.activate(chat=chat, actor=actor, revision_id=revision, digest=artifact.digest)
    worker.status = "execution_error"
    for _ in range(3):
        await service.run(chat=chat, actor=actor, revision_id=revision, arguments={})
    status = await repo.status(chat=chat, revision_id=revision)
    assert status[0]["status"] == "suspended" and status[0]["failures"] == 3
    assert await repo.list_active(chat=chat) == []

    worker.status = "ok"
    await service.activate(chat=chat, actor=actor, revision_id=revision, digest=artifact.digest)
    result = await service.run(chat=chat, actor=actor, revision_id=revision, arguments={})
    assert result.status == "ok"
    status = await repo.status(chat=chat, revision_id=revision)
    assert status[0]["status"] == "active" and status[0]["failures"] == 0


@pytest.mark.asyncio
async def test_worker_failure_is_recorded_as_unavailable() -> None:
    class BrokenWorker(_Worker):
        async def run(self, *, run_id, **kwargs):
            if run_id.startswith("test-"):
                return await super().run(**kwargs)
            raise RuntimeError("sensitive worker detail")

    chat, actor = ChatRef("tg", "-1001"), UserRef("tg", "7")
    artifact = Artifact("x", "def run(arguments): return 10", (ExampleCase({}, 10),))
    repo, worker = MemoryExtensionRepo(), BrokenWorker()
    service = ExtensionService(repo, _Generator(artifact), worker, enabled=lambda: True)
    request = await service.request(
        chat=chat, actor=actor, description="x", examples=artifact.cases
    )
    revision = await service.build(chat=chat, actor=actor, request_id=request)
    await service.activate(chat=chat, actor=actor, revision_id=revision, digest=artifact.digest)
    result = await service.run(chat=chat, actor=actor, revision_id=revision, arguments={})
    assert result.status == "unavailable" and result.value is None
    assert len(repo._runs) == 1
    assert next(iter(repo._runs.values()))["status"] == "unavailable"


@pytest.mark.asyncio
async def test_cancelled_invocation_finalizes_metadata_and_reraises() -> None:
    import asyncio

    class GateWorker(_Worker):
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, *, run_id, **kwargs):
            if run_id.startswith("test-"):
                return await super().run(**kwargs)
            self.started.set()
            await self.release.wait()
            return await super().run(run_id=run_id, **kwargs)

    chat, actor = ChatRef("tg", "-1001"), UserRef("tg", "7")
    artifact = Artifact("x", "def run(arguments): return 10", (ExampleCase({}, 10),))
    repo, worker = MemoryExtensionRepo(), GateWorker()
    service = ExtensionService(repo, _Generator(artifact), worker, enabled=lambda: True)
    request = await service.request(
        chat=chat, actor=actor, description="x", examples=artifact.cases
    )
    revision = await service.build(chat=chat, actor=actor, request_id=request)
    await service.activate(chat=chat, actor=actor, revision_id=revision, digest=artifact.digest)
    task = asyncio.create_task(
        service.run(chat=chat, actor=actor, revision_id=revision, arguments={})
    )
    await asyncio.wait_for(worker.started.wait(), timeout=2)
    await service.revoke(chat=chat, actor=actor, revision_id=revision)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runs = list(repo._runs.values())
    assert len(runs) == 1
    assert runs[0]["status"] == "unavailable" and runs[0].get("completed_at") is not None
    assert await repo.get_activation(chat=chat, revision_id=revision) is None
