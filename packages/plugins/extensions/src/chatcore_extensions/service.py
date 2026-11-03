from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable

from chatcore.events import ChatRef, UserRef

from chatcore_extensions.models import Artifact, ExampleCase, RunResult, canonical_json
from chatcore_extensions.repository import ExtensionRepo


class ExtensionServiceError(RuntimeError):
    """A fixed safe lifecycle failure."""


class ExtensionService:
    def __init__(self, repo: ExtensionRepo, generator, worker, *, enabled: Callable[[], bool]):
        self.repo = repo
        self.generator = generator
        self.worker = worker
        self.enabled = enabled

    async def request(
        self, *, chat: ChatRef, actor: UserRef, description: str, examples: tuple[ExampleCase, ...]
    ) -> str:
        if not self.enabled():
            raise ExtensionServiceError("extensions are disabled")
        return await self.repo.create_request(
            chat=chat, actor=actor, description=description, examples=examples
        )

    async def build(self, *, chat: ChatRef, actor: UserRef, request_id: str) -> str:
        if not self.enabled():
            raise ExtensionServiceError("extensions are disabled")
        request = await self.repo.get_request(chat=chat, request_id=request_id)
        if request is None or not await self.repo.claim_build(
            chat=chat, request_id=request_id, actor=actor
        ):
            raise ExtensionServiceError("request unavailable")
        try:
            artifact = await self.generator.generate(
                chat=chat,
                requester=request.author_key and UserRef.parse(request.author_key),
                description=request.description,
                examples=request.examples,
                build_id=request_id,
            )
            if not isinstance(artifact, Artifact):
                raise ExtensionServiceError("generation returned invalid artifact")
            revision_id = await self.repo.save_revision(
                chat=chat, request_id=request_id, actor=actor, artifact=artifact
            )
            results: list[RunResult] = []
            passed = True
            for index, case in enumerate(artifact.cases):
                result = await self.worker.run(
                    chat=chat,
                    run_id=f"test-{request_id}-{index}",
                    artifact=artifact,
                    arguments=case.arguments,
                )
                results.append(result)
                if result.status != "ok" or canonical_json(result.value) != canonical_json(
                    case.expected
                ):
                    passed = False
            first = results[0]
            same_identity = all(
                result.image_id == first.image_id
                and result.policy_digest == first.policy_digest
                and result.harness_digest == first.harness_digest
                for result in results
            )
            passed = passed and same_identity
            await self.repo.save_evidence(
                chat=chat,
                revision_id=revision_id,
                digest=artifact.digest,
                passed=passed,
                image_id=first.image_id,
                policy_digest=first.policy_digest,
                harness_digest=first.harness_digest,
                case_count=len(results),
            )
            if not passed:
                raise ExtensionServiceError("extension tests failed")
            return revision_id
        except Exception as exc:
            await self.repo.fail_build(chat=chat, request_id=request_id)
            if isinstance(exc, ExtensionServiceError):
                raise
            raise ExtensionServiceError("extension build failed") from None

    async def activate(
        self, *, chat: ChatRef, actor: UserRef, revision_id: str, digest: str
    ) -> None:
        if not self.enabled():
            raise ExtensionServiceError("extensions are disabled")
        revision = await self.repo.get_revision(chat=chat, revision_id=revision_id)
        evidence = await self.repo.get_evidence(chat=chat, revision_id=revision_id)
        if revision is None or evidence is None or revision.artifact.digest != digest:
            raise ExtensionServiceError("revision evidence unavailable")
        if not evidence.passed or evidence.digest != digest:
            raise ExtensionServiceError("revision tests did not pass")
        try:
            health = await self.worker.health()
        except Exception:
            raise ExtensionServiceError("worker unavailable") from None
        if (
            health.get("image_id") != evidence.image_id
            or health.get("policy_digest") != evidence.policy_digest
            or health.get("harness_digest") != evidence.harness_digest
        ):
            raise ExtensionServiceError("revision evidence is stale")
        if not await self.repo.activate(
            chat=chat, revision_id=revision_id, reviewer=actor, digest=digest
        ):
            raise ExtensionServiceError("activation rejected")

    async def revoke(self, *, chat: ChatRef, actor: UserRef, revision_id: str) -> None:
        await self.repo.revoke(chat=chat, revision_id=revision_id, reviewer=actor)

    async def run(
        self, *, chat: ChatRef, actor: UserRef, revision_id: str, arguments: dict[str, object]
    ) -> RunResult:
        evidence = await self.repo.get_evidence(chat=chat, revision_id=revision_id)
        if not self.enabled() or evidence is None:
            raise ExtensionServiceError("extension unavailable")
        run_id = secrets.token_hex(12)
        reserved = await self.repo.reserve_run(
            chat=chat, revision_id=revision_id, actor=actor, run_id=run_id
        )
        if reserved is None:
            raise ExtensionServiceError("extension unavailable")
        revision, epoch = reserved
        try:
            health = await self.worker.health()
            if (
                health.get("image_id") != evidence.image_id
                or health.get("policy_digest") != evidence.policy_digest
                or health.get("harness_digest") != evidence.harness_digest
            ):
                raise ExtensionServiceError("worker evidence is stale")
            result = await self.worker.run(
                chat=chat, run_id=run_id, artifact=revision.artifact, arguments=arguments
            )
        except asyncio.CancelledError:
            failed = RunResult(
                "unavailable",
                None,
                0,
                evidence.image_id,
                evidence.policy_digest,
                evidence.harness_digest,
            )
            finalize = asyncio.create_task(
                self.repo.record_run(
                    chat=chat,
                    revision_id=revision_id,
                    run_id=run_id,
                    digest=revision.artifact.digest,
                    epoch=epoch,
                    actor=actor,
                    result=failed,
                )
            )
            try:
                await asyncio.wait_for(asyncio.shield(finalize), timeout=3)
            except (Exception, asyncio.CancelledError):
                finalize.cancel()
            raise
        except Exception:
            # A reserved invocation must always become an auditable failed run.
            # Keep worker details private and feed the failure through the same
            # epoch/revocation checks as a normal result.
            result = RunResult(
                "unavailable",
                None,
                0,
                evidence.image_id,
                evidence.policy_digest,
                evidence.harness_digest,
            )
        if (
            result.image_id != evidence.image_id
            or result.policy_digest != evidence.policy_digest
            or result.harness_digest != evidence.harness_digest
        ):
            result = RunResult(
                "unavailable",
                None,
                result.duration_ms,
                evidence.image_id,
                evidence.policy_digest,
                evidence.harness_digest,
            )
        recorded = await self.repo.record_run(
            chat=chat,
            revision_id=revision_id,
            run_id=run_id,
            digest=revision.artifact.digest,
            epoch=epoch,
            actor=actor,
            result=result,
        )
        current = await self.repo.get_activation(chat=chat, revision_id=revision_id)
        if not recorded or current is None or current.epoch != epoch or not self.enabled():
            return RunResult(
                "unavailable",
                None,
                result.duration_ms,
                evidence.image_id,
                evidence.policy_digest,
                evidence.harness_digest,
            )
        return result
