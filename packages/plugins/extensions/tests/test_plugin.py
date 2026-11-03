from __future__ import annotations

import pytest
from chatcore.events import ChatRef
from chatcore.pending import PendingAction
from chatcore.plugin import CommandContext
from chatcore.testing import FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, ToolRegistry
from chatcore_extensions.authority import ExtensionAuthority
from chatcore_extensions.models import Artifact, ExampleCase
from chatcore_extensions.plugin import ExtensionsPlugin
from chatcore_extensions.repository import MemoryExtensionRepo
from chatcore_extensions.service import ExtensionService


class _Generator:
    async def generate(self, **_kwargs):
        raise AssertionError("not called")


class _Worker:
    async def run(self, **_kwargs):
        raise AssertionError("not called")


@pytest.fixture
async def context():
    stores = memory_stores()
    platform = FakePlatform()
    actor = user()
    chat = ChatRef("fake", "group")
    await stores.repo.upsert_member(chat, actor)

    async def admin(_chat, _user):
        return False

    authority = ExtensionAuthority(lambda target: target == chat, admin)
    repo = MemoryExtensionRepo()
    service = ExtensionService(repo, _Generator(), _Worker(), enabled=lambda: True)
    plugin = ExtensionsPlugin(repo, service, authority, enabled=lambda: True)
    return (
        plugin,
        ToolContext(chat, actor, False, platform, stores),
        CommandContext(chat, actor, False, False, platform, stores),
    )


@pytest.mark.asyncio
async def test_request_tool_only_creates_bound_confirmation(context) -> None:
    plugin, tool_ctx, cmd_ctx = context
    registry = ToolRegistry(plugin.tools())
    result = await registry.call(
        "request_extension",
        {
            "description": "Double a supplied integer.",
            "examples": [{"arguments": {"n": 2}, "expected": 4}],
        },
        tool_ctx,
    )
    assert not result.is_error and result.pending_action is not None
    assert "Request-confirmed examples" in result.pending_action.summary
    assert '"expected": 4' in result.pending_action.summary
    assert await plugin.repo.get_request(chat=tool_ctx.chat, request_id="missing") is None
    response = await plugin.commit(result.pending_action, cmd_ctx)
    assert response.startswith("Request ")
    assert result.pending_action.chat == tool_ctx.chat


@pytest.mark.asyncio
async def test_forged_foreign_request_confirmation_is_rejected(context) -> None:
    plugin, tool_ctx, cmd_ctx = context
    foreign = PendingAction.create(
        chat=ChatRef("fake", "other"),
        requester=tool_ctx.user.ref,
        plugin="extensions",
        kind="request",
        payload={"description": "leak", "examples": [{"arguments": {}, "expected": 1}]},
        summary="forged",
    )
    response = await plugin.commit(foreign, cmd_ctx)
    assert response == "This extension action is unavailable."
    assert not await plugin.repo.get_request(chat=tool_ctx.chat, request_id="anything")


@pytest.mark.asyncio
async def test_global_admin_flag_does_not_grant_group_management(context) -> None:
    plugin, tool_ctx, cmd_ctx = context
    assert not await plugin.authority.may_manage(
        chat=tool_ctx.chat, user=tool_ctx.user.ref, is_dm=False
    )
    response = await plugin._command(cmd_ctx, "revoke " + "a" * 24)
    assert response == "Only a current chat administrator can revoke extensions."


@pytest.mark.asyncio
async def test_dm_identity_is_private_scope(context) -> None:
    plugin, tool_ctx, cmd_ctx = context
    dm_user = user("dana")
    dm_chat = ChatRef("fake", "dana")
    assert await plugin.authority.may_manage(chat=dm_chat, user=dm_user.ref, is_dm=True)
    assert not await plugin.authority.may_manage(chat=tool_ctx.chat, user=dm_user.ref, is_dm=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revision_id", "arguments"),
    [
        ([], {}),
        ("not-a-revision", {}),
        ("a" * 24, []),
        ("a" * 24, {"x": "y" * 17_000}),
        ("a" * 24, {"x": [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]}),
    ],
)
async def test_invalid_run_tool_inputs_are_rejected_before_service_or_quota(
    context, revision_id, arguments
) -> None:
    plugin, tool_ctx, _ = context
    called = False

    async def run(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid input reached the service")

    plugin.service.run = run
    result = await plugin._run_tool(tool_ctx, revision_id=revision_id, arguments=arguments)
    assert result.is_error
    assert result.untrusted
    assert not called
    assert not plugin.repo._runs


@pytest.mark.asyncio
@pytest.mark.parametrize("description,examples", [(None, []), (7, []), ("valid", {})])
async def test_invalid_proposal_types_return_fixed_error(context, description, examples) -> None:
    plugin, tool_ctx, _ = context
    result = await plugin._request_tool(tool_ctx, description=description, examples=examples)
    assert result.is_error
    assert result.content == "Provide a short description and up to eight bounded JSON examples."


@pytest.mark.asyncio
async def test_review_pages_expose_complete_manifest_and_mark_source_untrusted(context) -> None:
    plugin, tool_ctx, cmd_ctx = context

    async def admin(_chat, _user):
        return True

    plugin.authority.group_admin = admin
    artifact = Artifact(
        "safe description",
        "def run(arguments):\n    # Tests: passing\n    # ``` [click](https://evil.example)\n"
        '    return "Image: fake"\n' + "# filler\n" * 500,
        (ExampleCase({"request": "confirmed"}, {"expected": True}),),
    )
    request = await plugin.repo.create_request(
        chat=cmd_ctx.chat,
        actor=cmd_ctx.user.ref,
        description="safe description",
        examples=artifact.cases,
    )
    await plugin.repo.claim_build(chat=cmd_ctx.chat, request_id=request, actor=cmd_ctx.user.ref)
    revision = await plugin.repo.save_revision(
        chat=cmd_ctx.chat, request_id=request, actor=cmd_ctx.user.ref, artifact=artifact
    )
    await plugin.repo.save_evidence(
        chat=cmd_ctx.chat,
        revision_id=revision,
        digest=artifact.digest,
        passed=True,
        image_id="sha256:" + "a" * 64,
        policy_digest="b" * 64,
        harness_digest="c" * 64,
        case_count=1,
    )
    first = await plugin._command(cmd_ctx, f"review {revision} 1")
    page_count = int(first.split("page 1/")[1].splitlines()[0])
    pages = [first]
    for page in range(2, page_count + 1):
        pages.append(await plugin._command(cmd_ctx, f"review {revision} {page}"))
    manifest = "".join(p.split("```json\n", 1)[1].rsplit("\n```", 1)[0] for p in pages)
    assert "safe description" in manifest
    assert "request-confirmed" in "".join(pages).lower()
    reviewed = __import__("json").loads(manifest)
    assert reviewed["artifact"]["cases"][0]["arguments"]["request"] == "confirmed"
    assert "# filler\n" * 500 in reviewed["artifact"]["source"]
    assert "Untrusted manifest JSON" in "".join(pages)
    assert "```[click]" not in "".join(pages)
    assert "Tests: passing" in first and artifact.digest in first
    assert "Image: sha256:" + "a" * 64 in first
    assert "Policy: " + "b" * 64 in first
    assert "Harness: " + "c" * 64 in first


@pytest.mark.asyncio
async def test_activation_confirmation_binds_scope_and_evidence_identity(context) -> None:
    from dataclasses import replace

    plugin, _, cmd_ctx = context

    async def admin(_chat, _user):
        return True

    class Confirm:
        action = None

        async def request(self, action, _user):
            self.action = action

    confirmation = Confirm()
    cmd_ctx.confirm = confirmation
    plugin.authority.group_admin = admin
    artifact = Artifact("x", "def run(arguments): return 1", (ExampleCase({}, 1),))
    request = await plugin.repo.create_request(
        chat=cmd_ctx.chat, actor=cmd_ctx.user.ref, description="x", examples=artifact.cases
    )
    await plugin.repo.claim_build(chat=cmd_ctx.chat, request_id=request, actor=cmd_ctx.user.ref)
    revision = await plugin.repo.save_revision(
        chat=cmd_ctx.chat, request_id=request, actor=cmd_ctx.user.ref, artifact=artifact
    )
    await plugin.repo.save_evidence(
        chat=cmd_ctx.chat,
        revision_id=revision,
        digest=artifact.digest,
        passed=True,
        image_id="sha256:" + "a" * 64,
        policy_digest="b" * 64,
        harness_digest="c" * 64,
        case_count=1,
    )
    await plugin._command(cmd_ctx, f"activate {revision} {artifact.digest}")
    action = confirmation.action
    assert action is not None
    assert action.payload["image_id"] == "sha256:" + "a" * 64
    assert action.payload["policy_digest"] == "b" * 64
    assert action.payload["harness_digest"] == "c" * 64
    assert action.payload["case_count"] == 1
    assert cmd_ctx.chat.key in action.summary
    assert artifact.digest in action.summary
    assert "Tests: passed (1 request-confirmed cases)" in action.summary
    assert "Image ID: sha256:" + "a" * 64 in action.summary
    plugin.repo._evidence[(cmd_ctx.chat.key, revision)] = replace(
        await plugin.repo.get_evidence(chat=cmd_ctx.chat, revision_id=revision),
        harness_digest="d" * 64,
    )
    response = await plugin.commit(action, cmd_ctx)
    assert "evidence changed" in response
    assert await plugin.repo.get_activation(chat=cmd_ctx.chat, revision_id=revision) is None


@pytest.mark.asyncio
async def test_build_confirmation_shows_complete_proposal_to_current_admin(context) -> None:
    plugin, _, cmd_ctx = context

    async def admin(_chat, _user):
        return True

    class Confirm:
        action = None

        async def request(self, action, _user):
            self.action = action

    confirmation = Confirm()
    cmd_ctx.confirm = confirmation
    plugin.authority.group_admin = admin
    cases = (ExampleCase({"input": 2}, 4),)
    request_id = await plugin.repo.create_request(
        chat=cmd_ctx.chat,
        actor=cmd_ctx.user.ref,
        description="Double this exact number.",
        examples=cases,
    )
    await plugin._command(cmd_ctx, f"build {request_id}")
    action = confirmation.action
    assert action is not None
    assert action.chat == cmd_ctx.chat
    assert "Double this exact number." in action.summary
    assert '"input": 2' in action.summary and '"expected": 4' in action.summary


@pytest.mark.asyncio
async def test_request_confirmation_escapes_markup_and_preserves_exact_payload(context) -> None:
    import json

    plugin, tool_ctx, _ = context
    result = await plugin._request_tool(
        tool_ctx,
        description="Review ``` [click](https://evil.example)",
        examples=[{"arguments": {"input": "```[link](https://evil.example)"}, "expected": 1}],
    )
    assert result.pending_action is not None and result.untrusted
    body = result.pending_action.summary.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    payload = json.loads(body)
    assert payload["description"] == "Review ``` [click](https://evil.example)"
    assert payload["examples"][0]["arguments"]["input"] == "```[link](https://evil.example)"
    assert "``` [click]" not in result.pending_action.summary


@pytest.mark.asyncio
async def test_status_limits_drafts_to_requester_or_current_administrator(context) -> None:
    plugin, _, cmd_ctx = context
    other = user("other")
    own_request = await plugin.repo.create_request(
        chat=cmd_ctx.chat,
        actor=cmd_ctx.user.ref,
        description="own private draft",
        examples=(ExampleCase({}, 1),),
    )
    other_request = await plugin.repo.create_request(
        chat=cmd_ctx.chat,
        actor=other.ref,
        description="other private draft",
        examples=(ExampleCase({}, 2),),
    )
    own_status = await plugin._command(cmd_ctx, "status")
    assert own_request in own_status
    assert other_request not in own_status
    assert "requester_key" not in own_status

    async def admin(_chat, _user):
        return True

    plugin.authority.group_admin = admin
    admin_status = await plugin._command(cmd_ctx, "status")
    assert own_request in admin_status and other_request in admin_status
    assert "private draft" not in admin_status


@pytest.mark.asyncio
async def test_draft_review_follows_requester_not_builder_attribution(context) -> None:
    plugin, tool_ctx, builder_ctx = context
    requester = user("requester")
    requester_ctx = CommandContext(
        builder_ctx.chat,
        requester,
        False,
        False,
        builder_ctx.platform,
        builder_ctx.stores,
    )
    artifact = Artifact("requester draft", "def run(arguments): return 1", (ExampleCase({}, 1),))
    request = await plugin.repo.create_request(
        chat=builder_ctx.chat,
        actor=requester.ref,
        description="requester draft",
        examples=artifact.cases,
    )
    await plugin.repo.claim_build(
        chat=builder_ctx.chat, request_id=request, actor=builder_ctx.user.ref
    )
    revision = await plugin.repo.save_revision(
        chat=builder_ctx.chat,
        request_id=request,
        actor=builder_ctx.user.ref,
        artifact=artifact,
    )

    assert (await plugin._command(requester_ctx, f"review {revision}")).startswith("Scope:")
    assert (
        await plugin._command(builder_ctx, f"review {revision}") == "You cannot review this draft."
    )

    async def current_admin(_chat, _user):
        return True

    plugin.authority.group_admin = current_admin
    assert (await plugin._command(builder_ctx, f"review {revision}")).startswith("Scope:")
