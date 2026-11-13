"""Resolve known people and freeze one confirmed, atomic inviter batch."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from chatcore.administration.permissions import is_owner_dm
from chatcore.administration.store import StaleChangeError
from chatcore.events import User, UserRef
from chatcore.pending import PendingAction
from chatcore.tools.registry import Tool, ToolResult

if TYPE_CHECKING:
    from chatcore.administration.plugin import AdministrationPlugin
    from chatcore.plugin import CommandContext
    from chatcore.tools.registry import ToolContext


log = logging.getLogger(__name__)


def build_tools(plugin: AdministrationPlugin) -> list[Tool]:
    async def manage(ctx, *, operation, people):
        return await request_batch(plugin, ctx, operation=operation, people=people)

    return [
        Tool(
            "manage_trusted_inviters",
            "Owner-only, private-DM invitation permission management for known people. "
            "Resolve names or @usernames and preview one confirmed batch. Trust lets them "
            "invite this bot to new groups; it grants no other admin role or content access. "
            "Untrust affects future invitations only. Never guess identities; "
            "ask if unknown or ambiguous.",
            {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["trust", "untrust"],
                        "description": "Trust grants invitation permission; untrust revokes it.",
                    },
                    "people": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "description": "People to change, as exact known names or @usernames.",
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": "Exact known name or @username; never an invented ID.",
                        },
                    },
                },
                "required": ["operation", "people"],
                "additionalProperties": False,
            },
            manage,
            writes=True,
            admin_only=True,
            context_allowed=lambda ctx: is_owner_dm(plugin.access, ctx),
        )
    ]


def _failure(text: str) -> ToolResult:
    return ToolResult(text, is_error=True, untrusted=True, persist_content=False)


def _label(user: User) -> str:
    name = " ".join(user.display_name.split())[:60] or user.ref.key
    handle = f" (@{user.username[:32]})" if user.username else ""
    return f"{name}{handle} [{user.ref.key}]"


async def resolve_people(
    plugin: AdministrationPlugin, ctx: ToolContext, people: list[str]
) -> list[User]:
    # Membership is live for this permission-management directory; never scan an
    # unrelated group merely because the requester is a bot owner.
    await plugin.access.refresh()
    directory = []
    for group in plugin.access.groups_on(ctx.platform.name):
        try:
            member = await ctx.platform.is_member(group, ctx.user.ref)
        except Exception as exc:
            log.warning("inviter directory membership check failed: %s", type(exc).__name__)
            continue
        if member:
            directory.extend(await ctx.stores.repo.find_members(group))
    selected = {}
    for person in people:
        value = person.strip().casefold()
        matches = {}
        for user in directory:
            if user.ref.platform != ctx.platform.name or user.is_bot:
                continue
            if value.startswith("@"):
                matched = user.username and user.username.casefold() == value[1:]
            else:
                matched = value in {
                    user.display_name.casefold(),
                    user.ref.key.casefold(),
                    user.username.casefold() if user.username else "",
                }
            if matched:
                matches[user.ref.key] = user
        if not matches:
            raise ValueError(
                f"Unknown person {person!r}. Use a known @username "
                "or have them speak in a shared group first."
            )
        if len(matches) != 1:
            candidates = "; ".join(_label(user) for user in list(matches.values())[:8])
            raise ValueError(
                f"Ambiguous person {person!r}: {candidates}. "
                "Specify the @username or known identity."
            )
        user = next(iter(matches.values()))
        if user.ref.key in plugin.owners:
            raise ValueError(
                "Owners already have invitation permission and cannot be trust targets."
            )
        selected[user.ref.key] = user
    return list(selected.values())


async def request_batch(
    plugin: AdministrationPlugin, ctx: ToolContext, *, operation: str, people: list[str]
) -> ToolResult:
    if not is_owner_dm(plugin.access, ctx):
        return _failure(
            "Invitation permissions can only be managed by an owner in their own private DM."
        )
    if type(operation) is not str or operation not in {"trust", "untrust"}:
        return _failure("Choose trust or untrust.")
    if (
        not isinstance(people, list)
        or not 1 <= len(people) <= 20
        or any(
            type(person) is not str or not person.strip() or len(person) > 200 for person in people
        )
    ):
        return _failure("Name between 1 and 20 people in one batch.")
    try:
        users = await resolve_people(plugin, ctx, people)
        snapshot = await plugin.store.snapshot()
    except ValueError as exc:
        return _failure(str(exc))
    users = [
        user
        for user in users
        if (user.ref.key in snapshot.trusted_inviters) != (operation == "trust")
    ]
    if not users:
        return _failure(
            "No changes needed: selected people already have the requested invitation permission."
        )
    effect = (
        f"Allow {len(users)} people to invite me to new groups:"
        if operation == "trust"
        else f"Remove invitation permission from {len(users)} people "
        "(existing groups stay enabled):"
    )
    summary = "Confirm: " + effect + "\n" + "\n".join(_label(user) for user in users)
    summary += "\nInvitation permission only; no admin, setup, moderation or content access."
    action = PendingAction.create(
        chat=ctx.chat,
        requester=ctx.user.ref,
        plugin=plugin.name,
        kind="inviter_batch",
        payload={
            "operation": operation,
            "people": [user.ref.key for user in users],
            "expected_revision": snapshot.revision,
        },
        summary=summary,
        thread_id=ctx.thread_id,
    )
    return ToolResult(summary, pending_action=action, untrusted=True, persist_content=False)


async def commit_batch(
    plugin: AdministrationPlugin, action: PendingAction, ctx: CommandContext
) -> str:
    if (
        not is_owner_dm(plugin.access, ctx)
        or action.chat != ctx.chat
        or action.requester != ctx.user.ref
        or action.plugin != plugin.name
        or action.kind != "inviter_batch"
    ):
        return "Invitation changes must be confirmed by their owner requester in the same DM."
    payload = action.payload
    if (
        set(payload) != {"operation", "people", "expected_revision"}
        or type(payload["expected_revision"]) is not int
        or payload["expected_revision"] < 0
        or type(payload["operation"]) is not str
        or not isinstance(payload["people"], list)
        or not 1 <= len(payload["people"]) <= 20
        or any(type(key) is not str for key in payload["people"])
    ):
        return "That confirmation is invalid. Start the request again."
    try:
        users = tuple(UserRef.parse(key) for key in payload["people"])
        await plugin.store.apply_inviter_batch(
            actor=ctx.user.ref,
            owners=plugin.owners,
            expected_revision=payload["expected_revision"],
            operation=payload["operation"],
            users=users,
            protected=plugin.protected,
        )
        await plugin.access.refresh()
    except StaleChangeError:
        return "Settings changed since this request. Ask again to confirm the latest batch."
    except PermissionError:
        return "This invitation change is no longer authorized."
    except ValueError:
        return "That confirmation is invalid. Start the request again."
    count = len(set(payload["people"]))
    return f"Saved {payload['operation']} invitation permission for {count} people."
