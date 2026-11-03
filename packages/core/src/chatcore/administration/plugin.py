"""Confirmed, deterministic DM commands for owner and group-scoped setup administration."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from chatcore.access import AccessPolicy
from chatcore.administration.store import (
    AdminChange,
    AdministrationStore,
    StaleChangeError,
    check_target,
    global_scope,
    is_global_scope,
    validate_change,
)
from chatcore.display_time import format_instant
from chatcore.display_time_store import display_service
from chatcore.events import ChatRef, UserRef
from chatcore.limits import (
    DAILY_BUDGET_MAX,
    DAILY_BUDGET_MIN,
    EXTRA_ALLOWANCE_MAX,
    EXTRA_ALLOWANCE_MIN,
    Limits,
)
from chatcore.pending import PendingAction
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.security import injection

USAGE = """Usage:
/admin groups | admins | moderators | restrictions | audit
/admin enable tg:-100123 | disable tg:-100123
/admin grant tg:123456 tg:-100123 | revoke tg:123456 tg:-100123
/admin mod tg:123456 tg:-100123 | unmod tg:123456 tg:-100123
/admin block tg:123456 [tg:-100123] | unblock tg:123456 [tg:-100123]
/admin pause tg:123456 2h [tg:-100123]
/admin limit tg:123456 50|default
/admin chatmode tg:-100123 on|off
/admin persona tg:-100123 Friendly and concise
/admin persona tg:-100123 reset
/admin usage | set N | default | extra N | extra clear"""


def _audit_display_detail(operation: str, detail: str, zone: str) -> str:
    if operation != "pause" or not detail.startswith("until="):
        return detail
    value = detail.removeprefix("until=")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return "until=" + format_instant(instant, zone)
    except ValueError:
        return detail


class AdministrationPlugin(BasePlugin):
    name = "administration"
    help_title = "Administration"

    def __init__(
        self,
        access: AccessPolicy,
        store: AdministrationStore,
        owners: frozenset[str],
        *,
        enable_chatmode: bool = True,
        protected: frozenset[str] = frozenset(),
        limits: Limits | None = None,
    ) -> None:
        self.access = access
        self.store = store
        self.owners = owners
        self.enable_chatmode = enable_chatmode
        self.protected = protected
        self.limits = limits

    def commands(self) -> list[Command]:
        return [Command("admin", "Configure groups and delegated setup access", self._command)]

    def action_handlers(self):
        return {"change": self}

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        return await self._commit(action, ctx)

    async def _command(self, ctx: CommandContext, args: str) -> str | None:
        owner = self.access.is_owner(ctx.user.ref)
        groups = self.access.assigned_groups(ctx.user.ref)
        moderated = self.access.moderated_groups(ctx.user.ref)
        if not owner and not groups and not moderated:
            return None
        raw = args.strip()
        # "usage" matches /usage; "budget" is the old name and keeps working.
        first = raw.split(maxsplit=1)[0].lower() if raw else ""
        is_budget = first in {"usage", "budget"}
        if is_budget:
            if not ctx.is_dm or ctx.chat.key != ctx.user.ref.key:
                return "Run /admin in a private chat with me to manage group setup."
            if not self.access.is_owner(ctx.user.ref):
                return "Owner access is required for this administration change."
            return await self._budget_command(ctx, raw.split())
        if not ctx.is_dm or ctx.chat.key != ctx.user.ref.key:
            return "Run /admin in a private chat with me to manage group setup."
        if not raw or raw == "help":
            return self._menu(owner, bool(groups), bool(moderated))
        parts = (
            raw.split(maxsplit=2) if raw.split(maxsplit=1)[0].lower() == "persona" else raw.split()
        )
        verb = parts[0].lower()
        if verb == "chatmode" and not self.enable_chatmode:
            return "Automatic group replies are disabled. Tag me when you want a group reply."
        if verb in {"groups", "admins", "moderators", "restrictions", "audit"}:
            if len(parts) != 1:
                return self._usage()
            if verb == "groups":
                return await self._groups(ctx, owner, groups)
            if verb == "admins":
                return await self._admins(ctx, owner, groups)
            if verb == "moderators":
                return await self._moderators(ctx, owner, moderated)
            if verb == "restrictions":
                return await self._restrictions(ctx, owner, moderated)
            return await self._audit(ctx, owner, groups | moderated)
        try:
            change = self._parse_change(ctx.platform.name, parts)
            validate_change(change)
        except (ValueError, IndexError):
            return self._usage()
        if change.operation in {
            "grant",
            "revoke",
            "moderator_grant",
            "moderator_revoke",
            "limit",
            "limit_reset",
        } or (change.operation in {"block", "pause", "unblock"} and is_global_scope(change.group)):
            if not owner:
                return "Owner access is required for this administration change."
        elif change.operation in {"block", "pause", "unblock"}:
            if not self.access.can_moderate(ctx.user.ref, change.group):
                return "Moderation access is required for this group."
        elif not self.access.can_setup(ctx.user.ref, change.group):
            return "Setup access is required for this group."
        if ctx.confirm is None:
            return "Administration confirmations are unavailable."
        snapshot = await self.store.snapshot()
        try:
            check_target(snapshot, ctx.user.ref, self.owners, self.protected, change)
        except ValueError:
            return "That person can't be the target of this change."
        target = change.user.key if change.user else None
        if change.operation == "persona":
            effect = f"set persona to: {change.value}"
        elif change.operation == "persona_reset":
            effect = "reset the persona"
        elif change.operation == "chatmode":
            effect = f"set chatmode {change.value}"
        elif change.operation in {"enable", "disable"}:
            effect = f"{change.operation} this group"
        elif change.operation in {"block", "unblock", "pause"}:
            scope = (
                "everywhere (global)" if is_global_scope(change.group) else f"in {change.group.key}"
            )
            if change.operation == "pause":
                effect = f"pause {target} {scope} for {_duration_label(change.value)}"
            else:
                effect = f"{change.operation} {target} {scope}"
            target = None
        elif change.operation == "limit":
            effect = f"set {target} hourly request limit to {change.value}"
            target = None
        elif change.operation == "limit_reset":
            effect = f"reset {target} hourly request limit to the default"
            target = None
        elif change.operation == "moderator_grant":
            effect = f"grant {target} moderation of {change.group.key}"
            target = None
        elif change.operation == "moderator_revoke":
            effect = f"revoke {target}'s moderation of {change.group.key}"
            target = None
        else:
            effect = f"{change.operation} setup access"
        scope_summary = (
            f" in {change.group.key}"
            if change.operation
            in {"enable", "disable", "chatmode", "persona", "persona_reset", "grant", "revoke"}
            else ""
        )
        action = PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin=self.name,
            kind="change",
            payload={
                "operation": change.operation,
                "group": change.group.key,
                "user": change.user.key if change.user else None,
                "value": change.value,
                "expected_revision": snapshot.revision,
            },
            summary=f"Confirm: {effect}{scope_summary}{f' for {target}' if target else ''}.",
            thread_id=ctx.thread_id,
        )
        await ctx.confirm.request(action, ctx.user)
        return None

    @staticmethod
    def _parse_change(platform: str, parts: list[str]) -> AdminChange:
        verb = parts[0].lower()
        if verb in {"enable", "disable"} and len(parts) == 2:
            return AdminChange(verb, _group(platform, parts[1]))
        if verb in {"grant", "revoke", "mod", "unmod"} and len(parts) == 3:
            operation = {"mod": "moderator_grant", "unmod": "moderator_revoke"}.get(verb, verb)
            return AdminChange(
                operation, _group(platform, parts[2]), user=_user(platform, parts[1])
            )
        if verb in {"block", "unblock"} and len(parts) in {2, 3}:
            group = _scope(platform, parts[2] if len(parts) == 3 else None)
            return AdminChange(verb, group, user=_user(platform, parts[1]))
        if verb == "pause" and len(parts) in {3, 4}:
            seconds = _parse_duration(parts[2])
            group = _scope(platform, parts[3] if len(parts) == 4 else None)
            return AdminChange("pause", group, user=_user(platform, parts[1]), value=seconds)
        if verb == "limit" and len(parts) == 3:
            if parts[2].lower() == "default":
                return AdminChange(
                    "limit_reset", global_scope(platform), user=_user(platform, parts[1])
                )
            try:
                value = int(parts[2])
            except ValueError as exc:
                raise ValueError("Limit must be a number or default") from exc
            return AdminChange(
                "limit", global_scope(platform), user=_user(platform, parts[1]), value=value
            )
        if verb == "chatmode" and len(parts) == 3:
            if parts[2] not in {"on", "off"}:
                raise ValueError("Expected on or off")
            return AdminChange(verb, _group(platform, parts[1]), value=parts[2])
        if verb == "persona" and len(parts) >= 3:
            value = parts[2].strip()
            if not value:
                raise ValueError("Persona text is required")
            if value == "reset":
                return AdminChange("persona_reset", _group(platform, parts[1]))
            return AdminChange("persona", _group(platform, parts[1]), value=value)
        raise ValueError("Unknown command")

    def _usage(self) -> str:
        return "\n".join(
            line
            for line in USAGE.splitlines()
            if self.enable_chatmode or not line.startswith("/admin chatmode")
        )

    def _menu(self, owner: bool, delegated: bool, moderator: bool) -> str:
        lines = ["/admin groups", "/admin audit"]
        if owner:
            lines.extend(
                [
                    "/admin admins",
                    "/admin moderators | /admin restrictions",
                    "/admin enable GROUP_ID and /admin disable GROUP_ID",
                    "/admin grant USER_ID GROUP_ID and /admin revoke USER_ID GROUP_ID",
                    "/admin mod USER_ID GROUP_ID and /admin unmod USER_ID GROUP_ID",
                    "/admin block USER_ID [GROUP_ID] | /admin pause USER_ID DURATION [GROUP_ID]",
                    "/admin unblock USER_ID [GROUP_ID]",
                    "/admin limit USER_ID N|default",
                    "/admin usage | set N | default | extra N | extra clear",
                ]
            )
        elif delegated:
            lines.extend(
                [
                    "/admin admins",
                    "/admin enable GROUP_ID and /admin disable GROUP_ID",
                ]
            )
        if moderator and not owner:
            lines.extend(
                [
                    "/admin moderators | /admin restrictions",
                    "/admin block USER_ID GROUP_ID | /admin pause USER_ID DURATION GROUP_ID",
                    "/admin unblock USER_ID GROUP_ID",
                ]
            )
        if owner or delegated:
            if self.enable_chatmode:
                lines.append("/admin chatmode GROUP_ID on|off")
            lines.append("/admin persona GROUP_ID TEXT (or reset)")
        return "Administration commands (numeric IDs):\n" + "\n".join(lines)

    async def _budget_command(self, ctx: CommandContext, parts: list[str]) -> str | None:
        if len(parts) == 1:
            return await self._budget_status(ctx)
        if self.limits is None:
            return "Budget administration is unavailable."
        if (
            len(parts) == 3
            and parts[1].lower() in {"set", "extra"}
            and not (parts[1].lower() == "extra" and parts[2].lower() == "clear")
        ):
            operation = "budget_set" if parts[1].lower() == "set" else "budget_extra"
            minimum, maximum, label = (
                (DAILY_BUDGET_MIN, DAILY_BUDGET_MAX, "Daily budget")
                if operation == "budget_set"
                else (EXTRA_ALLOWANCE_MIN, EXTRA_ALLOWANCE_MAX, "Extra allowance")
            )
            try:
                value = _parse_tokens(parts[2])
            except ValueError:
                return f"{label} must be between {minimum:,} and {maximum:,} tokens."
            if not minimum <= value <= maximum:
                return f"{label} must be between {minimum:,} and {maximum:,} tokens."
            change = AdminChange(
                operation,
                global_scope(ctx.platform.name),
                value=value,
                day=self.limits.day() if operation == "budget_extra" else None,
            )
        elif len(parts) == 2 and parts[1].lower() == "default":
            change = AdminChange("budget_reset", global_scope(ctx.platform.name))
        elif len(parts) == 3 and parts[1].lower() == "extra" and parts[2].lower() == "clear":
            change = AdminChange("budget_extra_reset", global_scope(ctx.platform.name))
        else:
            return self._usage()
        validate_change(change)
        if ctx.confirm is None:
            return "Administration confirmations are unavailable."
        snapshot = await self.store.snapshot()
        current = self.limits.base_budget()
        if change.operation == "budget_set":
            effect = f"set the daily token budget to {change.value:,} (currently {current:,})"
        elif change.operation == "budget_reset":
            effect = (
                "reset the daily token budget to the env default "
                f"({self.limits.default_budget():,})"
            )
        elif change.operation == "budget_extra":
            effect = f"add an extra {change.value:,} tokens for {change.day.isoformat()} only"
        else:
            effect = "clear today's extra token allowance"
        payload = {
            "operation": change.operation,
            "group": change.group.key,
            "user": None,
            "value": change.value,
            "expected_revision": snapshot.revision,
        }
        if change.operation == "budget_extra":
            payload["day"] = change.day.isoformat()
        action = PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin=self.name,
            kind="change",
            payload=payload,
            summary=f"Confirm: {effect}.",
            thread_id=ctx.thread_id,
        )
        await ctx.confirm.request(action, ctx.user)
        return None

    async def _budget_status(self, ctx: CommandContext) -> str:
        if self.limits is None:
            return "Budget administration is unavailable."
        limits = self.limits
        used = await limits.budget_used()
        now = datetime.now(ZoneInfo(limits.settings.BUDGET_TZ))
        base = limits.base_budget()
        extra = limits.extra_today(now)
        effective = limits.daily_budget(now)
        remaining = max(0, effective - used)
        zone, _ = await display_service(ctx.stores).resolve(ctx.chat, ctx.user.ref, is_dm=ctx.is_dm)
        override = self.access.budget_override()
        if override and override.daily_tokens is not None and base == override.daily_tokens:
            set_at = format_instant(override.set_at, zone) if override.set_at else "unknown time"
            source = f"Base: {base:,} (override by {override.set_by or 'unknown'} at {set_at})"
        else:
            source = f"Base: {base:,} (default from env)"
        lines = [
            "Daily token budget:",
            f"Used today: {used:,} / {effective:,} ({used / effective * 100:.1f}%)",
            source,
        ]
        if extra:
            lines.append(f"Extra today: {extra:,}")
        lines.extend(
            [
                f"Remaining: {remaining:,}",
                f"Resets: {format_instant(limits.reset_at(now), zone)} "
                f"(midnight {limits.settings.BUDGET_TZ})",
            ]
        )
        try:
            counter = await ctx.stores.cache.get(injection.counter_key())
            count = int(counter) if counter is not None else 0
        except Exception:
            count = 0
        lines.append(f"Injection flags today (UTC): {count:,}")
        return "\n".join(lines)

    async def _groups(self, ctx: CommandContext, owner: bool, assigned: frozenset[str]) -> str:
        configured = set(self.access.bootstrap_chats)
        if self.access.snapshot:
            configured.update(self.access.snapshot.overrides)
            configured.update(group for _, group in self.access.snapshot.assignments)
        visible = configured if owner else assigned
        keys = sorted(
            key
            for key in visible
            if not is_global_scope(group := ChatRef.parse(key))
            and not (group.platform == "tg" and not group.id.startswith("-"))
        )
        if not keys:
            return "No groups are configured." if owner else "No groups are assigned to you."
        lines = ["Groups:"]
        for key in keys:
            group = ChatRef.parse(key)
            enabled = self.access.is_chat_allowed(group)
            settings = await ctx.stores.repo.get_chat_settings(group)
            mode = "on" if settings.chatmode else "off"
            persona = "set" if settings.persona else "default"
            mode_text = f", chatmode {mode}" if self.enable_chatmode else ""
            lines.append(
                f"{key}: {'enabled' if enabled else 'disabled'}{mode_text}, persona {persona}"
            )
        return "\n".join(lines)

    async def _admins(self, ctx: CommandContext, owner: bool, assigned: frozenset[str]) -> str:
        snapshot = self.access.snapshot
        if snapshot is None:
            return "Administration state is unavailable."
        rows = sorted(
            (user, group)
            for user, group in snapshot.assignments
            if owner or (user == ctx.user.ref.key and group in assigned)
        )
        if not owner and not rows:
            return "No setup assignments."
        if not owner:
            return "Your setup assignments:\n" + "\n".join(
                f"{user} → {group}" for user, group in rows
            )
        lines = ["Owners: " + (", ".join(sorted(self.owners)) or "none")]
        full_admins = sorted(self.access.admin_users - self.owners)
        lines.append("Full admins: " + (", ".join(full_admins) or "none"))
        lines.append("Setup assignments:")
        lines.extend(f"{user} → {group}" for user, group in rows)
        lines.append("Moderators:")
        lines.extend(f"{user} → {group}" for user, group in sorted(snapshot.moderators))
        return "\n".join(lines)

    async def _moderators(self, ctx: CommandContext, owner: bool, moderated: frozenset[str]) -> str:
        snapshot = self.access.snapshot
        if snapshot is None:
            return "Administration state is unavailable."
        rows = sorted(
            (user, group)
            for user, group in snapshot.moderators
            if owner or (user == ctx.user.ref.key and group in moderated)
        )
        if not rows:
            return "No moderators are assigned." if owner else "You moderate no groups."
        return "Moderators:\n" + "\n".join(f"{user} → {group}" for user, group in rows)

    async def _restrictions(
        self, ctx: CommandContext, owner: bool, moderated: frozenset[str]
    ) -> str:
        snapshot = self.access.snapshot
        if snapshot is None:
            return "Administration state is unavailable."
        now = datetime.now(UTC)
        zone, _ = await display_service(ctx.stores).resolve(ctx.chat, ctx.user.ref, is_dm=ctx.is_dm)
        active = [
            row
            for row in snapshot.restrictions.values()
            if (owner or row.scope in moderated)
            and (row.kind == "block" or row.until is not None and row.until > now)
        ]
        lines = [
            f"{row.user} {('global' if is_global_scope(ChatRef.parse(row.scope)) else row.scope)} "
            f"{row.kind}" + (f" until {format_instant(row.until, zone)}" if row.until else "")
            for row in sorted(active, key=lambda item: (item.scope, item.user))
        ]
        if owner:
            lines.extend(
                f"{user} limit {limit}/hour" for user, limit in sorted(snapshot.user_limits.items())
            )
        return "Restrictions:\n" + ("\n".join(lines) if lines else "None")

    async def _audit(self, ctx: CommandContext, owner: bool, assigned: frozenset[str]) -> str:
        rows = await self.store.audit(groups=None if owner else assigned, limit=20)
        if not rows:
            return "No administration audit entries."
        zone, _ = await display_service(ctx.stores).resolve(ctx.chat, ctx.user.ref, is_dm=ctx.is_dm)
        return "Recent administration changes:\n" + "\n".join(
            f"r{row.revision} {row.actor} {row.operation} {row.group}"
            + (f" {row.subject}" if row.subject else "")
            + (f" {_audit_display_detail(row.operation, row.detail, zone)}" if row.detail else "")
            for row in rows
        )

    async def _commit(self, action: PendingAction, ctx: CommandContext) -> str:
        if (
            not ctx.is_dm
            or ctx.chat.key != ctx.user.ref.key
            or action.chat != ctx.chat
            or action.requester != ctx.user.ref
            or ctx.chat.platform != ctx.platform.name
            or ctx.user.ref.platform != ctx.platform.name
            or action.plugin != self.name
            or action.kind != "change"
        ):
            return "Administration changes must be confirmed by their requester in the same DM."
        payload = action.payload
        expected = {"operation", "group", "user", "value", "expected_revision"}
        operation_hint = payload.get("operation")
        if operation_hint == "budget_extra":
            expected.add("day")
        if (
            set(payload) != expected
            or type(payload["expected_revision"]) is not int
            or type(payload["operation"]) is not str
            or type(payload["group"]) is not str
            or (payload["user"] is not None and type(payload["user"]) is not str)
        ):
            return "That confirmation is invalid. Start the command again."
        try:
            operation = payload["operation"]
            if operation.startswith("budget_") and self.limits is None:
                return "Budget administration is unavailable."
            if operation == "chatmode" and not self.enable_chatmode:
                raise ValueError("Automatic group replies are disabled")
            group = _stored_scope(ctx.platform.name, payload["group"])
            change_day = None
            if operation == "budget_extra":
                if type(payload["day"]) is not str:
                    raise ValueError("Invalid budget date")
                change_day = date.fromisoformat(payload["day"])
                if self.limits is None:
                    return "Budget administration is unavailable."
                if change_day != self.limits.day():
                    return "Today's date changed since this request. Run the command again."
            user = (
                _user(ctx.platform.name, payload["user"]) if payload["user"] is not None else None
            )
            value = payload["value"]
            if operation not in {
                "enable",
                "disable",
                "grant",
                "revoke",
                "moderator_grant",
                "moderator_revoke",
                "block",
                "pause",
                "unblock",
                "limit",
                "limit_reset",
                "chatmode",
                "persona",
                "persona_reset",
                "budget_set",
                "budget_reset",
                "budget_extra",
                "budget_extra_reset",
            }:
                raise ValueError("Invalid operation")
            if value is not None and type(value) not in {str, bool, int}:
                raise ValueError("Invalid value")
            change = AdminChange(operation, group, user, value, change_day)
            validate_change(change)
            if operation.startswith("budget_") and not self.access.is_owner(ctx.user.ref):
                return "Owner access is required for this administration change."
            await self.store.apply(
                actor=ctx.user.ref,
                owners=self.owners,
                expected_revision=payload["expected_revision"],
                change=change,
                protected=self.protected,
            )
            await self.access.refresh()
        except StaleChangeError:
            return (
                "Settings changed since this request. Run the command again and confirm "
                "the latest change."
            )
        except PermissionError as exc:
            return f"Change not authorized: {exc}."
        except ValueError:
            return "That confirmation is invalid. Start the command again."
        scope = "everywhere" if is_global_scope(group) else f"for {group.key}"
        if operation == "budget_set":
            return f"Daily token budget saved: {value:,}."
        if operation == "budget_reset":
            default_budget = self.limits.default_budget()
            return f"Daily token budget reset to the env default ({default_budget:,})."
        if operation == "budget_extra":
            return f"Extra allowance saved: {value:,} tokens for today."
        if operation == "budget_extra_reset":
            return "Today's extra token allowance cleared."
        return f"Saved {operation} {scope}."


def _group(platform: str, value: str) -> ChatRef:
    if not isinstance(value, str):
        raise ValueError("Invalid group ID")
    group = ChatRef.parse(value)
    if group.platform != platform or is_global_scope(group):
        raise ValueError("Group must use the current platform")
    return group


def _scope(platform: str, value: str | None) -> ChatRef:
    return global_scope(platform) if value is None else _group(platform, value)


def _stored_scope(platform: str, value: str) -> ChatRef:
    if not isinstance(value, str):
        raise ValueError("Invalid scope")
    scope = ChatRef.parse(value)
    if scope.platform != platform:
        raise ValueError("Scope must use the current platform")
    return scope


def _user(platform: str, value: str) -> UserRef:
    if not isinstance(value, str):
        raise ValueError("Invalid user ID")
    user = UserRef.parse(value)
    if user.platform != platform:
        raise ValueError("User must use the current platform")
    return user


def _parse_duration(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([mhd])", value.lower())
    if not match:
        raise ValueError("Duration must look like 30m, 2h, or 7d")
    amount = int(match[1])
    seconds = amount * {"m": 60, "h": 3600, "d": 86400}[match[2]]
    if not 60 <= seconds <= 30 * 86400:
        raise ValueError("Duration must be from 1m to 30d")
    return seconds


def _duration_label(seconds: int) -> str:
    for unit, factor in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % factor == 0:
            return f"{seconds // factor}{unit}"
    return f"{seconds}s"


def _parse_tokens(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9_,]*", value):
        return int(value.replace("_", "").replace(",", ""))
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([km])", value.lower())
    if match is None:
        raise ValueError("Invalid token count")
    try:
        tokens = Decimal(match[1]) * (1_000 if match[2] == "k" else 1_000_000)
    except InvalidOperation as exc:
        raise ValueError("Invalid token count") from exc
    if tokens != tokens.to_integral_value():
        raise ValueError("Token count must be a whole number")
    return int(tokens)
