"""Confirmation-gated job operations shared by tools and slash commands."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from chatcore.access import AccessPolicy
from chatcore.display_time import format_instant
from chatcore.display_time_store import display_service
from chatcore.pending import PendingAction
from chatcore.platform import ChatPlatform
from chatcore.plugin import CommandContext
from chatcore.skills import render_hint
from chatcore.store.pg import PgDatabase
from chatcore.tools.registry import ToolContext

from chatcore_jobs.models import Actor, JobKind, Scope
from chatcore_jobs.permissions import can_use_scope, is_group_manager, make_actor, make_scope
from chatcore_jobs.repository import JobConflict, JobsRepo
from chatcore_jobs.settings import GroupLimits, JobsSettings
from chatcore_jobs.timing import parse_reminder_time

DISABLED_HINTS = {
    JobKind.REMINDER: (
        "Reminders are off in this group. A group setup manager, the group creator or an admin "
        "can turn them on: they can ask me to turn on reminders, or send {cmd:jobtools} "
        "reminders on."
    ),
    JobKind.WATCH: (
        "Website watches are off in this group. A group setup manager, the group creator or an "
        "admin can turn them on: they can ask me to turn on website watches, or send "
        "{cmd:jobtools} watches on."
    ),
}
"""Shared by the skill gate (routing hint) and the call-time refusal, so both say the same."""

_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


def _validate_watch_url(url: object, settings: JobsSettings, *, rendered: bool = False) -> str:
    if not isinstance(url, str) or len(url) > settings.url_max_chars:
        raise ValueError("URL must be a string within the length limit")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid URL") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("only public HTTP and HTTPS URLs are supported")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL credentials are not supported")
    if port not in {None, 80, 443}:
        raise ValueError("only ports 80 and 443 are supported")
    if parts.fragment:
        raise ValueError("URL fragments are not supported")
    if rendered and parts.scheme.lower() == "http":
        host = parts.hostname
        authority = f"[{host}]" if ":" in host else host
        if port not in {None, 80}:
            authority += f":{port}"
        url = urlunsplit(("https", authority, parts.path, parts.query, ""))
    # Preserve the user's exact string so URL provenance checks compare the
    # same value the conversation actually contained.
    return url


class JobsService:
    def __init__(
        self,
        settings: JobsSettings,
        access: AccessPolicy,
        platform: ChatPlatform,
        clock=lambda: datetime.now(UTC),
        rendered_allowed=None,
        rendered_available: bool = False,
    ) -> None:
        self.settings, self.access, self.platform, self.clock = settings, access, platform, clock
        self.rendered_allowed, self.rendered_available = rendered_allowed, rendered_available

    def repo(self, stores: Any) -> JobsRepo:
        if not isinstance(stores.db, PgDatabase):
            raise RuntimeError("durable jobs require PostgreSQL")
        return JobsRepo(stores.db, self.settings)

    async def _allowed(
        self, scope: Scope, actor: Actor, *, allow_disabled_dm: bool = False
    ) -> bool:
        return await can_use_scope(
            scope,
            actor,
            access=self.access,
            platform=self.platform,
            dm_enabled=self.settings.dm_enabled,
            require_dm_enabled=not allow_disabled_dm,
        )

    async def request_create(
        self,
        ctx: ToolContext | CommandContext,
        kind: JobKind,
        data: dict[str, Any],
        *,
        url_provenance: bool = False,
    ) -> PendingAction:
        scope = make_scope(ctx.chat, is_dm=ctx.is_dm, thread_id=ctx.thread_id)
        actor = make_actor(ctx.user.ref)
        if not await self._allowed(scope, actor):
            raise PermissionError("Jobs are not available in this conversation.")
        repo = self.repo(ctx.stores)
        current = await repo.settings(scope)
        enabled = current["reminders_enabled" if kind is JobKind.REMINDER else "watches_enabled"]
        if not enabled:
            raise ValueError(self._disabled_text(kind, ctx))
        if kind is JobKind.REMINDER:
            text = data.get("text")
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > self.settings.reminder_max_chars
            ):
                raise ValueError("Reminder text must be 1–1000 characters.")
            parsed = parse_reminder_time(
                data.get("local_time"),
                data.get("timezone"),
                self.clock(),
                horizon_days=self.settings.reminder_horizon_days,
            )
            content = {"text": text.strip()}
            details, due = f"at {parsed.label}", parsed.instant
        else:
            rendered = data.get("rendered", False)
            if type(rendered) is not bool:
                raise ValueError("Rendered must be boolean.")
            if rendered:
                if not self.rendered_available or self.rendered_allowed is None:
                    raise ValueError("Rendered watches are unavailable.")
                try:
                    allowed_rendered = await self.rendered_allowed(scope)
                except Exception:
                    allowed_rendered = False
                if not allowed_rendered:
                    raise ValueError("Rendered watches are disabled for this group.")
                if self.settings.max_rendered_watches_global == 0:
                    raise ValueError("Rendered watches are disabled.")
            url = _validate_watch_url(data.get("url"), self.settings, rendered=rendered)
            interval = data.get("interval_seconds")
            if type(interval) is not int or interval > self.settings.maximum_watch_interval_seconds:
                raise ValueError("Watch interval is outside the configured bounds.")
            group_min = (
                current.get("minimum_interval_seconds")
                or self.settings.minimum_watch_interval_seconds
            )
            effective_min = max(
                group_min,
                self.settings.minimum_watch_interval_seconds,
                self.settings.minimum_rendered_watch_interval_seconds if rendered else 0,
            )
            if rendered and effective_min > self.settings.maximum_watch_interval_seconds:
                raise ValueError("Rendered watches are unavailable with current interval limits.")
            if interval < effective_min:
                raise ValueError(
                    f"Watch interval must be at least {effective_min} seconds in this group."
                )
            if url_provenance and data.get("url") not in ctx.turn.seen_urls:
                raise ValueError("The website URL must have appeared in this conversation first.")
            if rendered:
                try:
                    rendered_count = await repo.count_active_rendered_watches()
                except Exception:
                    raise ValueError("Rendered watch capacity could not be checked.") from None
                if rendered_count >= self.settings.max_rendered_watches_global:
                    raise ValueError("The global rendered watch limit has been reached.")
            content = {"url": url, "interval_seconds": interval}
            if rendered:
                content["mode"] = "rendered"
            details, due = f"for {url} every {interval} seconds", self.clock().astimezone(UTC)
        payload = {
            "scope": {
                "chat_key": scope.chat_key,
                "is_dm": scope.is_dm,
                "thread_id": scope.thread_id,
            },
            "actor": actor.user_key,
            "kind": kind.value,
            "data": content,
            "due_at": due.isoformat(),
            "revision": current["revision"],
        }
        destination = scope.chat_key + (f" topic {scope.thread_id}" if scope.thread_id else "")
        behavior = (
            "First check is silent; only readable text changes are reported."
            if kind is JobKind.WATCH
            else "Delivery status will be shown if the platform response is uncertain."
        )
        summary = f"Create {kind.value} {details} in {destination}. Retained for "
        summary += f"{self.settings.retention_days} days after completion. {behavior}"
        if kind is JobKind.WATCH and content.get("mode") == "rendered":
            summary = (
                f"Create rendered (headless browser) watch for {url} every {interval} seconds "
                f"in {destination}. First check is silent; only readable-text changes are "
                f"reported. Sites that block automated access will stop the watch with a notice. "
                f"Retained for {self.settings.retention_days} days after completion."
            )
        return PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin="jobs",
            kind="create",
            payload=payload,
            summary=summary,
            thread_id=ctx.thread_id,
            now=self.clock(),
        )

    @staticmethod
    def _disabled_text(kind: JobKind, ctx: ToolContext | CommandContext) -> str:
        if ctx.is_dm:
            return (
                "Reminders are off here."
                if kind is JobKind.REMINDER
                else ("Website watches are off here.")
            )
        return render_hint(DISABLED_HINTS[kind], ctx.command_form)

    async def request_cancel(
        self, ctx: ToolContext | CommandContext, kind: JobKind, raw_id: object
    ) -> PendingAction | None:
        if not isinstance(raw_id, str) or not _ID.fullmatch(raw_id):
            raise ValueError("A valid job ID is required.")
        scope = make_scope(ctx.chat, is_dm=ctx.is_dm, thread_id=ctx.thread_id)
        actor = make_actor(ctx.user.ref)
        if not await self._allowed(scope, actor, allow_disabled_dm=True):
            return None
        job = await self.repo(ctx.stores).get_job(scope, UUID(raw_id))
        if job is None or job.kind is not kind or job.status.value != "active":
            return None
        all_members = False
        if job.owner != actor:
            all_members = await is_group_manager(self.access, self.platform, scope, actor)
            if not all_members:
                return None
        payload = {
            "scope": {
                "chat_key": scope.chat_key,
                "is_dm": scope.is_dm,
                "thread_id": scope.thread_id,
            },
            "actor": actor.user_key,
            "kind": kind.value,
            "job_id": raw_id,
            "revision": job.revision,
            "all_members": all_members,
        }
        return PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin="jobs",
            kind="cancel",
            payload=payload,
            summary=f"Cancel {kind.value} {raw_id} in {scope.chat_key}"
            + (f" topic {scope.thread_id}" if scope.thread_id else "")
            + ". If a notification has already been reserved, it may still arrive.",
            thread_id=ctx.thread_id,
            now=self.clock(),
        )

    async def request_restore(
        self, ctx: ToolContext | CommandContext, kind: JobKind, raw_id: object
    ) -> PendingAction | None:
        if not isinstance(raw_id, str) or not _ID.fullmatch(raw_id):
            raise ValueError("A valid job ID is required.")
        scope = make_scope(ctx.chat, is_dm=ctx.is_dm, thread_id=ctx.thread_id)
        actor = make_actor(ctx.user.ref)
        if not await self._allowed(scope, actor):
            return None
        repo = self.repo(ctx.stores)
        current = await repo.settings(scope)
        enabled = current["reminders_enabled" if kind is JobKind.REMINDER else "watches_enabled"]
        if not enabled:
            raise ValueError(self._disabled_text(kind, ctx))
        job = await repo.get_job(scope, UUID(raw_id))
        if (
            job is None
            or job.kind is not kind
            or job.status.value != "cancelled"
            or job.last_delivery_state != "cancelled_by_user"
            or job.terminal_at is None
            or (self.clock() - job.terminal_at).total_seconds()
            > self.settings.restore_window_seconds
        ):
            return None
        all_members = False
        if job.owner != actor:
            all_members = await is_group_manager(self.access, self.platform, scope, actor)
            if not all_members:
                return None
        if kind is JobKind.REMINDER:
            if job.next_run_at <= self.clock():
                raise ValueError("That reminder's time has passed; create a new one.")
        else:
            rendered = job.payload.get("mode", "plain") == "rendered"
            _validate_watch_url(job.payload.get("url"), self.settings, rendered=rendered)
            interval = job.payload.get("interval_seconds")
            minimum = max(
                current["minimum_interval_seconds"],
                self.settings.minimum_watch_interval_seconds,
                self.settings.minimum_rendered_watch_interval_seconds if rendered else 0,
            )
            if (
                type(interval) is not int
                or not minimum <= interval <= self.settings.maximum_watch_interval_seconds
            ):
                raise ValueError(
                    f"Watch interval must be at least {minimum} seconds in this group."
                )
            if rendered:
                if not self.rendered_available or self.rendered_allowed is None:
                    raise ValueError("Rendered watches are unavailable.")
                try:
                    allowed_rendered = await self.rendered_allowed(scope)
                except Exception:
                    allowed_rendered = False
                if not allowed_rendered:
                    raise ValueError("Rendered watches are disabled for this group.")
                if self.settings.max_rendered_watches_global == 0:
                    raise ValueError("Rendered watches are disabled.")
                try:
                    rendered_count = await repo.count_active_rendered_watches()
                except Exception:
                    raise ValueError("Rendered watch capacity could not be checked.") from None
                if rendered_count >= self.settings.max_rendered_watches_global:
                    raise ValueError("The global rendered watch limit has been reached.")
        payload = {
            "scope": {
                "chat_key": scope.chat_key,
                "is_dm": scope.is_dm,
                "thread_id": scope.thread_id,
            },
            "actor": actor.user_key,
            "kind": kind.value,
            "job_id": raw_id,
            "revision": job.revision,
            "all_members": all_members,
            "settings_revision": current["revision"],
        }
        return PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin="jobs",
            kind="restore",
            payload=payload,
            summary=f"Restore {kind.value} {raw_id} in {scope.chat_key}.",
            thread_id=ctx.thread_id,
            now=self.clock(),
        )

    async def request_configure(
        self,
        ctx: ToolContext | CommandContext,
        kind: JobKind,
        enabled: object,
        limits: dict[str, Any],
    ) -> PendingAction:
        scope = make_scope(ctx.chat, is_dm=ctx.is_dm, thread_id=ctx.thread_id)
        actor = make_actor(ctx.user.ref)
        if not await is_group_manager(self.access, self.platform, scope, actor):
            raise PermissionError(
                "Only a current setup manager, group creator, or administrator can change settings."
            )
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        current = await self.repo(ctx.stores).settings(scope)
        if set(limits) - {"max_jobs_per_user", "max_jobs_per_chat", "minimum_interval_seconds"}:
            raise ValueError("Unknown setting")
        values = {}
        for key in ("max_jobs_per_user", "max_jobs_per_chat", "minimum_interval_seconds"):
            value = limits.get(key, current[key])
            if type(value) is not int:
                raise ValueError(f"{key} must be an integer")
            values[key] = value
        validated = GroupLimits(**values)
        validated._validate_ceilings(self.settings)
        payload = {
            "scope": {"chat_key": scope.chat_key, "is_dm": False, "thread_id": scope.thread_id},
            "actor": actor.user_key,
            "kind": kind.value,
            "enabled": enabled,
            "limits": values,
            "revision": current["revision"],
        }
        cancel_count = await self.repo(ctx.stores).count_active(scope, kind) if not enabled else 0
        cancellation_note = (
            f" Disabling permanently cancels {cancel_count} active {kind.value} job(s) "
            "across all topics; re-enabling will not restore them. The commit reply reports "
            "the number actually cancelled."
            if not enabled
            else ""
        )
        return PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin="jobs",
            kind="configure",
            payload=payload,
            summary=f"Set {kind.value}s {'on' if enabled else 'off'} in {scope.chat_key}; limits: "
            f"{validated.max_jobs_per_user} per user, {validated.max_jobs_per_chat} per group, "
            f"minimum watch interval {validated.minimum_interval_seconds} seconds."
            f"{cancellation_note}",
            thread_id=ctx.thread_id,
            now=self.clock(),
        )

    async def list_jobs(
        self, ctx: ToolContext | CommandContext, kind: JobKind, all_members: object = False
    ) -> str:
        if type(all_members) is not bool:
            raise ValueError("all_members must be boolean")
        scope = make_scope(ctx.chat, is_dm=ctx.is_dm, thread_id=ctx.thread_id)
        actor = make_actor(ctx.user.ref)
        if not await self._allowed(scope, actor):
            return "No jobs found."
        if all_members and not await is_group_manager(self.access, self.platform, scope, actor):
            return "Group administrator access is required for this list."
        jobs = await self.repo(ctx.stores).list_jobs(scope, actor, kind, all_members=all_members)
        if not jobs:
            return "No jobs found."
        zone, _ = await display_service(ctx.stores).resolve(ctx.chat, ctx.user.ref, is_dm=ctx.is_dm)
        return "\n".join(
            f"{job.id} · {job.status.value} · {format_instant(job.next_run_at, zone)} · "
            f"{job.last_delivery_state or 'no delivery'}"
            + (
                " · rendered (stops if group disables rendered fetch)"
                if kind is JobKind.WATCH and job.payload.get("mode", "plain") == "rendered"
                else ""
            )
            for job in jobs
        )

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        if action.chat != ctx.chat or action.requester != ctx.user.ref:
            return "This request no longer matches its original conversation. Nothing changed."
        p = action.payload
        try:
            raw_scope = p["scope"]
            if set(raw_scope) != {"chat_key", "is_dm", "thread_id"}:
                raise ValueError
            scope = Scope(raw_scope["chat_key"], raw_scope["is_dm"], raw_scope["thread_id"])
            actor, kind = Actor(p["actor"]), JobKind(p["kind"])
            if (
                scope.chat_key != action.chat.key
                or scope.is_dm != ctx.is_dm
                or scope.thread_id != action.thread_id
            ):
                raise ValueError
            if actor.user_key != action.requester.key or not await self._allowed(
                scope, actor, allow_disabled_dm=action.kind == "cancel"
            ):
                raise ValueError
        except Exception:
            return "Authorization or scope changed. Nothing was saved."
        repo = self.repo(ctx.stores)
        try:
            if action.kind == "create":
                current = await repo.settings(scope)
                enabled = current[
                    "reminders_enabled" if kind is JobKind.REMINDER else "watches_enabled"
                ]
                if not enabled or current["revision"] != p["revision"]:
                    return (
                        "Job settings changed while this was waiting. Nothing was saved; "
                        "request it again."
                    )
                payload = p["data"]
                if kind is JobKind.REMINDER:
                    due = parse_reminder_time(
                        p["due_at"],
                        "UTC",
                        self.clock(),
                        horizon_days=self.settings.reminder_horizon_days,
                    ).instant
                else:
                    rendered = payload.get("mode", "plain") == "rendered"
                    if payload.get("mode", "plain") not in {"plain", "rendered"}:
                        raise ValueError
                    if rendered and (
                        not self.rendered_available
                        or self.rendered_allowed is None
                        or self.settings.max_rendered_watches_global == 0
                    ):
                        return "Rendered watches are no longer available. Nothing was saved."
                    if rendered:
                        try:
                            allowed_rendered = await self.rendered_allowed(scope)
                        except Exception:
                            allowed_rendered = False
                        if not allowed_rendered:
                            return "Rendered watches are no longer available. Nothing was saved."
                    _validate_watch_url(payload["url"], self.settings, rendered=rendered)
                    interval = payload["interval_seconds"]
                    if type(interval) is not int:
                        raise ValueError
                    minimum = max(
                        current["minimum_interval_seconds"],
                        self.settings.minimum_watch_interval_seconds,
                        self.settings.minimum_rendered_watch_interval_seconds if rendered else 0,
                    )
                    if not minimum <= interval <= self.settings.maximum_watch_interval_seconds:
                        raise ValueError("watch interval outside current bounds")
                    due = self.clock().astimezone(UTC)
                job = await repo.create(
                    scope,
                    actor,
                    action.id,
                    kind,
                    payload,
                    due,
                    revision=p["revision"],
                    now=self.clock(),
                )
                zone, _ = await display_service(ctx.stores).resolve(
                    ctx.chat, ctx.user.ref, is_dm=ctx.is_dm
                )
                when = format_instant(job.next_run_at, zone)
                if kind is JobKind.REMINDER:
                    return (
                        f'Reminder set for {when}: "{job.payload["text"]}" (id {job.id}). '
                        'Say "undo" to cancel it.'
                    )
                rendered_note = (
                    " (headless browser)" if job.payload.get("mode") == "rendered" else ""
                )
                return (
                    f"Watching {job.payload['url']}{rendered_note} every "
                    f"{job.payload['interval_seconds']} seconds (id {job.id}). "
                    "The first check is silent; only readable-text changes are reported. "
                    'Say "undo" to stop it.'
                )
            if action.kind == "cancel":
                owner = await repo.get_job(scope, UUID(p["job_id"]))
                all_members = p["all_members"]
                if owner is None or owner.revision != p["revision"] or owner.kind is not kind:
                    return "No matching active job was found."
                if owner.owner != actor and (
                    not all_members
                    or not await is_group_manager(self.access, self.platform, scope, actor)
                ):
                    return "No matching active job was found."
                cancelled = await repo.cancel(
                    scope,
                    actor,
                    kind,
                    owner.id,
                    all_members=all_members,
                    expected_revision=p["revision"],
                )
                if not cancelled:
                    return "No matching active job was found."
                zone, _ = await display_service(ctx.stores).resolve(
                    ctx.chat, ctx.user.ref, is_dm=ctx.is_dm
                )
                reserved_note = " A notification already reserved by the worker may still arrive."
                if kind is JobKind.REMINDER:
                    due = format_instant(owner.next_run_at, zone)
                    return (
                        f'Cancelled reminder {owner.id} ("{owner.payload["text"]}", '
                        f'was due {due}). Say "undo" to bring it back.{reserved_note}'
                    )
                return (
                    f"Stopped watching {owner.payload['url']} (id {owner.id}). "
                    f'Say "undo" to restart it.{reserved_note}'
                )
            if action.kind == "restore":
                current = await repo.settings(scope)
                enabled = current[
                    "reminders_enabled" if kind is JobKind.REMINDER else "watches_enabled"
                ]
                if not enabled:
                    return self._disabled_text(kind, ctx)
                if current["revision"] != p["settings_revision"]:
                    return (
                        "Job settings changed while this was waiting. Nothing was changed; "
                        "request it again."
                    )
                owner = await repo.get_job(scope, UUID(p["job_id"]))
                all_members = p["all_members"]
                if owner is None or owner.revision != p["revision"] or owner.kind is not kind:
                    return "No matching cancelled job was found."
                if owner.owner != actor and (
                    not all_members
                    or not await is_group_manager(self.access, self.platform, scope, actor)
                ):
                    return "No matching cancelled job was found."
                if kind is JobKind.REMINDER:
                    if owner.next_run_at <= self.clock():
                        return "That reminder's time has passed; create a new one."
                else:
                    rendered = owner.payload.get("mode", "plain") == "rendered"
                    try:
                        _validate_watch_url(
                            owner.payload.get("url"), self.settings, rendered=rendered
                        )
                    except ValueError:
                        return "The website watch is no longer valid. Nothing was changed."
                    interval = owner.payload.get("interval_seconds")
                    minimum = max(
                        current["minimum_interval_seconds"],
                        self.settings.minimum_watch_interval_seconds,
                        self.settings.minimum_rendered_watch_interval_seconds if rendered else 0,
                    )
                    if (
                        type(interval) is not int
                        or not minimum <= interval <= self.settings.maximum_watch_interval_seconds
                    ):
                        return (
                            "The website watch interval is outside current bounds. "
                            "Nothing was changed."
                        )
                    if rendered:
                        if not self.rendered_available or self.rendered_allowed is None:
                            return "Rendered watches are no longer available. Nothing was changed."
                        try:
                            allowed_rendered = await self.rendered_allowed(scope)
                        except Exception:
                            allowed_rendered = False
                        if not allowed_rendered or self.settings.max_rendered_watches_global == 0:
                            return "Rendered watches are no longer available. Nothing was changed."
                restored = await repo.restore(
                    scope,
                    actor,
                    kind,
                    owner.id,
                    all_members=all_members,
                    expected_revision=p["revision"],
                    revision=p["settings_revision"],
                    now=self.clock(),
                )
                if restored is None:
                    return "No matching cancelled job was found."
                zone, _ = await display_service(ctx.stores).resolve(
                    ctx.chat, ctx.user.ref, is_dm=ctx.is_dm
                )
                when = format_instant(restored.next_run_at, zone)
                if kind is JobKind.REMINDER:
                    return (
                        f'Restored reminder {restored.id}: "{restored.payload["text"]}" '
                        f'at {when}. Say "undo" to cancel it.'
                    )
                return (
                    f"Restarted watching {restored.payload['url']} every "
                    f"{restored.payload['interval_seconds']} seconds (id {restored.id}). "
                    'Say "undo" to stop it.'
                )
            if action.kind == "configure":
                if not await is_group_manager(self.access, self.platform, scope, actor):
                    return "Current group setup authorization is required. Nothing changed."
                limits = GroupLimits(**p["limits"])
                result = await repo.configure(
                    scope,
                    kind=kind,
                    enabled=p["enabled"],
                    limits=limits,
                    expected_revision=p["revision"],
                )
                enabled_key = "reminders_enabled" if kind is JobKind.REMINDER else "watches_enabled"
                message = (
                    f"{kind.value}s {'enabled' if result[enabled_key] else 'disabled'} "
                    f"for {scope.chat_key}."
                )
                if p["enabled"] is False:
                    message += (
                        f" Permanently cancelled {result.get('_cancelled_jobs', 0)} active job(s); "
                        "re-enabling will not restore them."
                    )
                return message
        except (JobConflict, ValueError, KeyError, TypeError):
            return (
                "Job settings changed or the request was invalid. Nothing changed; "
                "please request it again."
            )
        return "Unknown job action."


class JobActions:
    def __init__(self, service: JobsService) -> None:
        self.service = service

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        return await self.service.commit(action, ctx)
