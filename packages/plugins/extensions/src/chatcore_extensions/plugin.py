from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from chatcore.events import ChatRef, UserRef
from chatcore.pending import PendingAction, PendingActionHandler
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.skills import Skill
from chatcore.store.base import Stores
from chatcore.tools.registry import Tool, ToolContext, ToolResult

from chatcore_extensions.authority import ExtensionAuthority
from chatcore_extensions.models import ArtifactValidationError, ExampleCase, canonical_json
from chatcore_extensions.repository import ExtensionRepo
from chatcore_extensions.service import ExtensionService, ExtensionServiceError

_ID = re.compile(r"^[0-9a-f]{24}$")
MAX_PROPOSAL_CONFIRMATION_BYTES = 3000
REVIEW_PAGE_CHARS = 800


# Default behaviour text for the `extensions` skill; apps may add to or override it.
EXTENSIONS_INSTRUCTIONS = (
    "The requester confirms an extension request and its examples before submission."
)


class ExtensionsPlugin(BasePlugin):
    name = "extensions"
    help_title = "Extensions"

    def skills(self):
        return [
            Skill(
                "extensions",
                "Request and run personal extensions",
                (
                    "make an extension",
                    "run my extension",
                    "创建扩展",
                    "运行我的扩展",
                    "can build this helper lah",
                ),
                ("request_extension", "list_extensions", "run_extension"),
                keywords=("extension", "扩展", "插件"),
                max_steps=3,
                instructions=EXTENSIONS_INSTRUCTIONS,
            )
        ]

    migrations_package = "chatcore_extensions.migrations"

    def __init__(
        self,
        repo: ExtensionRepo,
        service: ExtensionService,
        authority: ExtensionAuthority,
        *,
        enabled=lambda: False,
    ):
        self.repo = repo
        self.service = service
        self.authority = authority
        self.enabled = enabled

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "request_extension",
                "Request a reusable calculation, such as weighted bill splitting: JSON in/out, "
                "no network or chat access. Requires confirmation.",
                {
                    "type": "object",
                    "properties": {
                        "description": {
                            "description": "Reusable calculation the extension should perform.",
                            "type": "string",
                            "maxLength": 4096,
                        },
                        "examples": {
                            "description": "Required: 1–8 JSON input/output examples.",
                            "type": "array",
                            "maxItems": 8,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "arguments": {
                                        "description": "JSON input object for this calculation.",
                                        "type": "object",
                                    },
                                    "expected": {
                                        "description": "Expected JSON output for this example.",
                                    },
                                },
                                "required": ["arguments", "expected"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["description", "examples"],
                    "additionalProperties": False,
                },
                self._request_tool,
                writes=True,
            ),
            Tool(
                "list_extensions",
                "List approved reusable JSON calculations, such as weighted bill splits; "
                "no network or chat access.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                self._list_tool,
            ),
            Tool(
                "run_extension",
                "Run an approved JSON calculation, such as a weighted bill split; "
                "no network or chat access.",
                {
                    "type": "object",
                    "properties": {
                        "revision_id": {
                            "description": "Approved revision ID from list_extensions.",
                            "type": "string",
                        },
                        "arguments": {
                            "description": "JSON input object for this calculation.",
                            "type": "object",
                        },
                    },
                    "required": ["revision_id", "arguments"],
                    "additionalProperties": False,
                },
                self._run_tool,
            ),
        ]

    def commands(self) -> list[Command]:
        return [Command("extensions", "Manage scoped transformation extensions", self._command)]

    def action_handlers(self) -> dict[str, PendingActionHandler]:
        return {"request": self, "build": self, "activate": self}

    async def _request_tool(
        self, ctx: ToolContext, *, description: str, examples: list[dict[str, Any]]
    ) -> ToolResult:
        if not self.enabled() or not _scope_ok(self.authority, ctx.chat, ctx.user.ref, ctx.is_dm):
            return ToolResult.error("Extensions are unavailable in this chat.")
        try:
            if type(description) is not str or not isinstance(examples, list):
                raise ValueError
            cases = _parse_examples(examples)
            if not description.strip() or len(description.encode("utf-8")) > 4096:
                raise ValueError
            payload = {
                "description": description.strip(),
                "examples": [{"arguments": c.arguments, "expected": c.expected} for c in cases],
            }
            payload_bytes = canonical_json(payload)
            if len(payload_bytes) > MAX_PROPOSAL_CONFIRMATION_BYTES:
                raise ValueError
        except (ValueError, TypeError, UnicodeEncodeError, ArtifactValidationError):
            return ToolResult(
                "Provide a short description and up to eight bounded JSON examples.",
                untrusted=True,
                is_error=True,
            )
        summary_payload = {
            "provenance": (
                "request-confirmed examples; supplied through the conversational assistant"
            ),
            **payload,
        }
        examples_text = json.dumps(
            summary_payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).replace("`", "\\u0060")
        summary = (
            f"Confirm proposal in scope {ctx.chat.key}.\n"
            "Request-confirmed examples (assistant-provided; confirm this complete JSON):\n"
            f"```json\n{examples_text}\n```"
        )
        if len(summary.encode("utf-8")) > 3800:
            return ToolResult(
                "Provide a short description and up to eight bounded JSON examples.",
                untrusted=True,
                is_error=True,
            )
        action = PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin=self.name,
            kind="request",
            payload=payload,
            summary=summary,
            thread_id=ctx.thread_id,
        )
        return ToolResult(content=action.summary, pending_action=action, untrusted=True)

    async def _list_tool(self, ctx: ToolContext) -> ToolResult:
        if not self.enabled() or not _scope_ok(self.authority, ctx.chat, ctx.user.ref, ctx.is_dm):
            return ToolResult.error("Extensions are unavailable in this chat.")
        items = await self.repo.list_active(chat=ctx.chat)
        return ToolResult(
            content=json.dumps(
                [
                    {
                        "revision_id": item.id,
                        "description": item.artifact.description,
                        "digest": item.artifact.digest,
                    }
                    for item in items
                ],
                ensure_ascii=False,
            ),
            untrusted=True,
        )

    async def _run_tool(
        self, ctx: ToolContext, *, revision_id: str, arguments: dict[str, object]
    ) -> ToolResult:
        if not self.enabled() or not _scope_ok(self.authority, ctx.chat, ctx.user.ref, ctx.is_dm):
            return ToolResult.error("Extensions are unavailable in this chat.")
        if type(revision_id) is not str or not _ID.fullmatch(revision_id):
            return ToolResult("Invalid extension request.", untrusted=True, is_error=True)
        if not isinstance(arguments, dict):
            return ToolResult("Invalid extension request.", untrusted=True, is_error=True)
        try:
            if len(canonical_json(arguments)) > 16 * 1024:
                return ToolResult("Invalid extension request.", untrusted=True, is_error=True)
        except (ValueError, TypeError, UnicodeEncodeError, RecursionError):
            return ToolResult("Invalid extension request.", untrusted=True, is_error=True)
        try:
            result = await self.service.run(
                chat=ctx.chat, actor=ctx.user.ref, revision_id=revision_id, arguments=arguments
            )
        except ExtensionServiceError:
            return ToolResult(
                "The extension is unavailable or has been revoked.", untrusted=True, is_error=True
            )
        return ToolResult(
            json.dumps(
                {"status": result.status, "value": result.value},
                ensure_ascii=False,
                allow_nan=False,
            ),
            untrusted=True,
            is_error=result.status != "ok",
        )

    async def _command(self, ctx: CommandContext, args: str) -> str | None:
        if not self.enabled() or not _scope_ok(self.authority, ctx.chat, ctx.user.ref, ctx.is_dm):
            return "Extensions are unavailable in this chat."
        tokens = args.split()
        if not tokens or tokens[0] == "list" and len(tokens) == 1:
            items = await self.repo.list_active(chat=ctx.chat)
            return (
                "No approved extensions in this chat."
                if not items
                else "\n".join(
                    f"{item.id} · {item.artifact.description[:120]} · {item.artifact.digest}"
                    for item in items
                )
            )
        command = tokens[0]
        if command == "build" and len(tokens) == 2 and _ID.fullmatch(tokens[1]):
            if not await self.authority.may_manage(
                chat=ctx.chat, user=ctx.user.ref, is_dm=ctx.is_dm
            ):
                return "Only a current chat administrator can build extensions."
            if ctx.confirm is None:
                return "Confirmation is unavailable."
            request = await self.repo.get_request(chat=ctx.chat, request_id=tokens[1])
            if request is None or request.state != "requested":
                return "Request unavailable."
            proposal = {
                "description": request.description,
                "examples": [
                    {"arguments": case.arguments, "expected": case.expected}
                    for case in request.examples
                ],
            }
            proposal_digest = hashlib.sha256(canonical_json(proposal)).hexdigest()
            visible_proposal = json.dumps(
                {
                    "provenance": "request-confirmed examples; conversational assistant supplied",
                    **proposal,
                },
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            ).replace("`", "\\u0060")
            summary = (
                f"Build candidate in scope {ctx.chat.key}; proposal SHA-256: {proposal_digest}\n"
                f"Request-confirmed proposal:\n```json\n{visible_proposal}\n```"
            )
            if len(summary.encode("utf-8")) > 3800:
                return "The confirmed proposal is too large to display for build confirmation."
            action = self._action(
                ctx,
                "build",
                {"request_id": tokens[1], "proposal_digest": proposal_digest},
                summary,
            )
            await ctx.confirm.request(action, ctx.user)
            return "Build request sent for confirmation."
        if command == "review" and len(tokens) in (2, 3) and _ID.fullmatch(tokens[1]):
            page = _page(tokens[2]) if len(tokens) == 3 else 1
            if page is None:
                return "Page must be a positive integer."
            return await self._review(ctx, tokens[1], page)
        if (
            command == "activate"
            and len(tokens) == 3
            and _ID.fullmatch(tokens[1])
            and re.fullmatch(r"[0-9a-f]{64}", tokens[2])
        ):
            if not await self.authority.may_manage(
                chat=ctx.chat, user=ctx.user.ref, is_dm=ctx.is_dm
            ):
                return "Only a current chat administrator can activate extensions."
            if ctx.confirm is None:
                return "Confirmation is unavailable."
            revision = await self.repo.get_revision(chat=ctx.chat, revision_id=tokens[1])
            evidence = await self.repo.get_evidence(chat=ctx.chat, revision_id=tokens[1])
            if (
                revision is None
                or evidence is None
                or not evidence.passed
                or revision.artifact.digest != tokens[2]
                or evidence.digest != tokens[2]
            ):
                return "Passing evidence for this exact digest is unavailable."
            summary = (
                f"Activate extension in scope {ctx.chat.key}.\n"
                f"Revision: {tokens[1]}\nArtifact SHA-256: {tokens[2]}\n"
                f"Tests: passed ({evidence.case_count} request-confirmed cases)\n"
                f"Image ID: {evidence.image_id}\nPolicy SHA-256: {evidence.policy_digest}\n"
                f"Harness SHA-256: {evidence.harness_digest}"
            )
            action = self._action(
                ctx,
                "activate",
                {
                    "revision_id": tokens[1],
                    "digest": tokens[2],
                    "image_id": evidence.image_id,
                    "policy_digest": evidence.policy_digest,
                    "harness_digest": evidence.harness_digest,
                    "case_count": evidence.case_count,
                },
                summary,
            )
            await ctx.confirm.request(action, ctx.user)
            return "Activation request sent for confirmation."
        if command == "revoke" and len(tokens) == 2 and _ID.fullmatch(tokens[1]):
            if not await self.authority.may_manage(
                chat=ctx.chat, user=ctx.user.ref, is_dm=ctx.is_dm
            ):
                return "Only a current chat administrator can revoke extensions."
            await self.service.revoke(chat=ctx.chat, actor=ctx.user.ref, revision_id=tokens[1])
            return "Extension revoked."
        if (
            command == "status"
            and len(tokens) <= 2
            and (len(tokens) == 1 or _ID.fullmatch(tokens[1]))
        ):
            manager = await self.authority.may_manage(
                chat=ctx.chat, user=ctx.user.ref, is_dm=ctx.is_dm
            )
            status = await self.repo.status(
                chat=ctx.chat,
                revision_id=tokens[1] if len(tokens) == 2 else None,
                viewer=ctx.user.ref,
                include_drafts=manager,
            )
            return (
                json.dumps(
                    [
                        {key: value for key, value in row.items() if key != "requester_key"}
                        for row in status
                    ],
                    default=str,
                )
                if status
                else "No extension status in this chat."
            )
        return (
            "Usage: /extensions list | build ID | review ID [PAGE] | "
            "activate ID FULL_DIGEST | revoke ID | status [ID]"
        )

    async def _review(self, ctx: CommandContext, revision_id: str, page: int) -> str:
        revision = await self.repo.get_revision(chat=ctx.chat, revision_id=revision_id)
        if revision is None:
            return "Extension not found in this chat."
        manager = await self.authority.may_manage(chat=ctx.chat, user=ctx.user.ref, is_dm=ctx.is_dm)
        request = await self.repo.get_request(chat=ctx.chat, request_id=revision.request_id)
        if not manager and (request is None or request.author_key != ctx.user.ref.key):
            return "You cannot review this draft."
        evidence = await self.repo.get_evidence(chat=ctx.chat, revision_id=revision_id)
        manifest = json.dumps(
            {
                "case_provenance": (
                    "request-confirmed examples only; generated examples excluded in v1"
                ),
                "artifact": json.loads(revision.artifact.canonical),
            },
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).replace("`", "\\u0060")
        chunks = [
            manifest[i : i + REVIEW_PAGE_CHARS] for i in range(0, len(manifest), REVIEW_PAGE_CHARS)
        ] or [""]
        index = page - 1
        if index >= len(chunks):
            return f"Source has {len(chunks)} page(s)."
        tests = "passing" if evidence and evidence.passed else "failed or missing"
        identity = (
            f"Image: {evidence.image_id}\nPolicy: {evidence.policy_digest}\n"
            f"Harness: {evidence.harness_digest}\nCases: {evidence.case_count}"
            if evidence
            else "Evidence: unavailable"
        )
        return (
            f"Scope: {ctx.chat.key}\nExtension: {revision.id}\nDigest: {revision.artifact.digest}\n"
            f"Tests: {tests}\nLimits: 5s, 128 MiB, 0.5 CPU, 32 PIDs, 16 MiB /tmp\n"
            f"{identity}\nCase provenance: request-confirmed examples only; "
            "no independent generated cases in v1.\n"
            f"Untrusted manifest JSON — page {page}/{len(chunks)}\n```json\n"
            f"{chunks[index]}\n```"
        )

    def _action(
        self, ctx: CommandContext, kind: str, payload: dict[str, object], summary: str
    ) -> PendingAction:
        return PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin=self.name,
            kind=kind,
            payload=payload,
            summary=summary,
            thread_id=ctx.thread_id,
        )

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        if (
            action.plugin != self.name
            or action.chat != ctx.chat
            or action.requester != ctx.user.ref
            or not self.enabled()
            or not _scope_ok(self.authority, ctx.chat, ctx.user.ref, ctx.is_dm)
        ):
            return "This extension action is unavailable."
        payload = action.payload
        if action.kind == "request":
            if set(payload) != {"description", "examples"} or not isinstance(
                payload["description"], str
            ):
                return "Invalid request."
            try:
                description = payload["description"].strip()
                if not description or len(description.encode("utf-8")) > 4096:
                    raise ValueError
                examples = _parse_examples(payload["examples"])
                request_id = await self.service.request(
                    chat=ctx.chat,
                    actor=ctx.user.ref,
                    description=description,
                    examples=examples,
                )
            except (ValueError, TypeError, ArtifactValidationError, ExtensionServiceError):
                return "The extension request could not be saved."
            return f"Request {request_id} saved in this chat. A current administrator can build it."
        if action.kind == "build":
            if (
                set(payload) != {"request_id", "proposal_digest"}
                or not isinstance(payload["request_id"], str)
                or not isinstance(payload["proposal_digest"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload["proposal_digest"])
            ):
                return "Invalid build request."
            if not await self.authority.may_manage(
                chat=ctx.chat, user=ctx.user.ref, is_dm=ctx.is_dm
            ):
                return "Current administrator status is required."
            request = await self.repo.get_request(chat=ctx.chat, request_id=payload["request_id"])
            if request is None or request.state != "requested":
                return "Build failed or the request was already handled."
            proposal = {
                "description": request.description,
                "examples": [
                    {"arguments": case.arguments, "expected": case.expected}
                    for case in request.examples
                ],
            }
            if hashlib.sha256(canonical_json(proposal)).hexdigest() != payload["proposal_digest"]:
                return "The request changed; inspect it and confirm a new build."
            try:
                revision_id = await self.service.build(
                    chat=ctx.chat, actor=ctx.user.ref, request_id=payload["request_id"]
                )
            except ExtensionServiceError:
                return "Build failed or the request was already handled."
            return f"Built revision {revision_id}. Review its source and digest before activation."
        if action.kind == "activate":
            if (
                set(payload)
                != {
                    "revision_id",
                    "digest",
                    "image_id",
                    "policy_digest",
                    "harness_digest",
                    "case_count",
                }
                or not isinstance(payload["revision_id"], str)
                or not isinstance(payload["digest"], str)
                or not isinstance(payload["image_id"], str)
                or not isinstance(payload["policy_digest"], str)
                or not isinstance(payload["harness_digest"], str)
                or type(payload["case_count"]) is not int
                or not await self.authority.may_manage(
                    chat=ctx.chat, user=ctx.user.ref, is_dm=ctx.is_dm
                )
            ):
                return "Current administrator status is required."
            evidence = await self.repo.get_evidence(
                chat=ctx.chat, revision_id=payload["revision_id"]
            )
            revision = await self.repo.get_revision(
                chat=ctx.chat, revision_id=payload["revision_id"]
            )
            if (
                evidence is None
                or revision is None
                or not evidence.passed
                or revision.artifact.digest != payload["digest"]
                or evidence.digest != payload["digest"]
                or evidence.image_id != payload["image_id"]
                or evidence.policy_digest != payload["policy_digest"]
                or evidence.harness_digest != payload["harness_digest"]
                or evidence.case_count != payload["case_count"]
            ):
                return "Review evidence changed; inspect the current revision before activation."
            try:
                await self.service.activate(
                    chat=ctx.chat,
                    actor=ctx.user.ref,
                    revision_id=payload["revision_id"],
                    digest=payload["digest"],
                )
            except ExtensionServiceError:
                return "Activation failed; review the current evidence and full digest."
            return "Extension activated in this chat."
        return "Invalid extension action."

    async def purge_user(self, chat: ChatRef, user: UserRef, stores: Stores) -> None:
        await self.repo.purge_user(chat=chat, user=user)

    async def purge_chat(self, chat: ChatRef, stores: Stores) -> None:
        await self.repo.purge_chat(chat=chat)

    async def purge_older_than(self, cutoff: datetime, stores: Stores) -> None:
        await self.repo.purge_older_than(cutoff)


def _parse_examples(value: object) -> tuple[ExampleCase, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError
    cases = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"arguments", "expected"}:
            raise ValueError
        cases.append(ExampleCase(item["arguments"], item["expected"]))
    return tuple(cases)


def _page(value: str) -> int | None:
    if not value.isdecimal() or len(value) > 3:
        return None
    page = int(value)
    return page if page > 0 else None


def _scope_ok(authority: ExtensionAuthority, chat: ChatRef, user: UserRef, is_dm: bool) -> bool:
    if is_dm:
        return chat.key == user.key
    try:
        return bool(authority.scope_allowed(chat))
    except Exception:
        return False
