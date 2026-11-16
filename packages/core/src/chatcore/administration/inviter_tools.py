"""Resolve known people and freeze one confirmed, atomic inviter batch."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from chatcore.administration.permissions import is_owner_dm
from chatcore.administration.store import StaleChangeError
from chatcore.display_time import format_instant
from chatcore.display_time_store import display_service
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

    async def manage_dm(ctx, *, operation, people, duration=None):
        return await request_dm_batch(
            plugin, ctx, operation=operation, people=people, duration=duration
        )

    inviter_tool = Tool(
        "manage_trusted_inviters",
        "Owner DM only: manage known people's invitation permissions. "
        "Resolve names or @usernames and preview one confirmed batch. Trust lets them "
        "invite this bot to groups; no other admin role or content access. "
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
    return [
        inviter_tool,
        Tool(
            "manage_dm_access",
            "Owner DM only: stage a confirmed batch of private DM grants or revocations.",
            {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["grant", "revoke"],
                        "description": "Grant or revoke private DM access.",
                    },
                    "people": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "description": (
                            "Exact names; explicit tg: IDs only if typed by the owner."
                        ),
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": (
                                "Known name or tg:<positive id> typed by the owner; never invented."
                            ),
                        },
                    },
                    "duration": {
                        "type": "string",
                        "description": (
                            "Optional grant duration such as 12h or 7d, from 1m to 365d."
                        ),
                    },
                },
                "required": ["operation", "people"],
                "additionalProperties": False,
            },
            manage_dm,
            writes=True,
            admin_only=True,
            context_allowed=lambda ctx: is_owner_dm(plugin.access, ctx),
        ),
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


async def request_dm_batch(
    plugin: AdministrationPlugin,
    ctx: ToolContext,
    *,
    operation: str,
    people: list[str],
    duration: str | None,
) -> ToolResult:
    if not is_owner_dm(plugin.access, ctx):
        return _failure("DM access can only be managed by an owner in their private DM.")
    if type(operation) is not str or operation not in {"grant", "revoke"}:
        return _failure("Choose grant or revoke.")
    if operation == "revoke" and duration is not None:
        return _failure("Duration is only valid when granting access.")
    if (
        not isinstance(people, list)
        or not 1 <= len(people) <= 20
        or any(
            type(person) is not str or not person.strip() or len(person) > 200 for person in people
        )
    ):
        return _failure("Name between 1 and 20 people in one batch.")
    expiry_value = None
    try:
        if duration is not None:
            match = re.fullmatch(r"([1-9][0-9]*)([mhd])", duration.lower())
            if not match:
                raise ValueError("Use a duration from 1m to 365d.")
            seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
            if not 60 <= seconds <= 365 * 86400:
                raise ValueError("Use a duration from 1m to 365d.")
            expiry_value = (
                (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
            )
        users: dict[str, User] = {}
        for person in people:
            identity = re.fullmatch(r"tg:([1-9][0-9]*)", person.strip())
            if identity:
                if ctx.platform.name != "tg":
                    raise ValueError("Explicit tg IDs are only valid on Telegram.")
                ref = UserRef("tg", identity[1])
                if ref.key in plugin.owners or ref.key == ctx.user.ref.key:
                    raise ValueError("Owners and the requester cannot be DM grant targets.")
                users.setdefault(ref.key, User(ref, f"unknown name [{ref.key}]"))
            else:
                resolved = await resolve_people(plugin, ctx, [person])
                user = resolved[0]
                if user.ref.key == ctx.user.ref.key:
                    raise ValueError("The requester cannot be a DM grant target.")
                users.setdefault(user.ref.key, user)
        snapshot = await plugin.store.snapshot()
    except ValueError as exc:
        return _failure(str(exc))
    expiry = datetime.fromisoformat(expiry_value.replace("Z", "+00:00")) if expiry_value else None
    selected = []
    for user in users.values():
        grant = snapshot.dm_grants.get(user.ref.key)
        if operation == "grant" and (not grant or grant.expires_at != expiry):
            selected.append(user)
        elif operation == "revoke" and grant:
            selected.append(user)
    if not selected:
        return _failure("No changes needed: everyone already has the requested DM access state.")
    explicit_keys = {
        "tg:" + match[1]
        for person in people
        if (match := re.fullmatch(r"tg:([1-9][0-9]*)", person.strip()))
    }
    labels = [
        f"unknown name [{user.ref.key}]" if user.ref.key in explicit_keys else _label(user)
        for user in selected
    ]
    zone, _ = await display_service(ctx.stores).resolve(ctx.chat, ctx.user.ref, is_dm=True)
    expiry_label = format_instant(expiry, zone) if expiry else "until revoked"
    effect = (
        f"Grant private DM access to {len(selected)} people until {expiry_label}:"
        if operation == "grant"
        else f"Remove private DM access from {len(selected)} people:"
    )
    summary = "Confirm: " + effect + "\n" + "\n".join(labels)
    summary += (
        "\nPrivate DM only; no group access or admin role. Their usage counts toward the "
        "daily budget."
    )
    action = PendingAction.create(
        chat=ctx.chat,
        requester=ctx.user.ref,
        plugin=plugin.name,
        kind="dm_batch",
        payload={
            "operation": "dm_grant" if operation == "grant" else "dm_revoke",
            "people": [user.ref.key for user in selected],
            "value": expiry_value,
            "expected_revision": snapshot.revision,
        },
        summary=summary,
        thread_id=ctx.thread_id,
    )
    return ToolResult(summary, pending_action=action, untrusted=True, persist_content=False)


async def commit_dm_batch(
    plugin: AdministrationPlugin, action: PendingAction, ctx: CommandContext
) -> str:
    if (
        not is_owner_dm(plugin.access, ctx)
        or action.chat != ctx.chat
        or action.requester != ctx.user.ref
        or action.plugin != plugin.name
        or action.kind != "dm_batch"
    ):
        return "DM access changes must be confirmed by their owner requester in the same DM."
    payload = action.payload
    if (
        set(payload) != {"operation", "people", "value", "expected_revision"}
        or payload["operation"] not in {"dm_grant", "dm_revoke"}
        or not isinstance(payload["people"], list)
        or not 1 <= len(payload["people"]) <= 20
        or type(payload["expected_revision"]) is not int
        or (payload["value"] is not None and type(payload["value"]) is not str)
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
            value=payload["value"],
            protected=plugin.protected,
        )
        await plugin.access.refresh()
    except StaleChangeError:
        return "Settings changed since this request. Ask again to confirm the latest batch."
    except (PermissionError, ValueError):
        return "That DM access change is no longer valid. Start the request again."
    return f"Saved {payload['operation']} for {len(set(payload['people']))} people."
