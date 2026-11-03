"""Framework integration for confirmation-gated reusable jobs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from chatcore.access import AccessPolicy
from chatcore.command_prompts import CommandReply
from chatcore.events import ChatRef, UserRef
from chatcore.pending import PendingAction
from chatcore.platform import ChatPlatform
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.skills import Skill
from chatcore.store.base import Stores
from chatcore.tools.registry import Tool, ToolContext, ToolResult

from chatcore_jobs.gates import build_gates
from chatcore_jobs.models import Job, JobKind
from chatcore_jobs.permissions import can_use_scope
from chatcore_jobs.repository import JobsRepo
from chatcore_jobs.service import JobActions, JobsService
from chatcore_jobs.settings import JobsSettings
from chatcore_jobs.watch import RenderedAllowed, RenderedFetcher
from chatcore_jobs.worker import JobsWorker

log = logging.getLogger(__name__)


# Default behaviour text for the `reminders` skill; apps may add to or override it.
REMINDERS_INSTRUCTIONS = (
    "Reminders are created or cancelled immediately and the bot posts the result; do not "
    "repeat it. After creation, undo means cancel by the echoed ID; after cancellation, "
    "use restore_reminder."
)

# Default behaviour text for the `watches` skill; apps may add to or override it.
WATCHES_INSTRUCTIONS = (
    "Website watches are created or cancelled immediately and the bot posts the result; do "
    "not repeat it. After creation, undo means cancel by the echoed ID; after "
    "cancellation, use restore_website_watch."
)


class JobsPlugin(BasePlugin):
    name = "jobs"
    explainer_package = "chatcore_jobs"
    help_title = "Reminders and watches"
    migrations_package = "chatcore_jobs.migrations"

    def skills(self):
        return [
            Skill(
                "reminders",
                "Create and manage reminders",
                (
                    "remind me tomorrow",
                    "list my reminders",
                    "明天提醒我",
                    "取消提醒",
                    "remind me later can or not",
                    "restore cancelled reminder can?",
                ),
                ("create_reminder", "list_reminders", "cancel_reminder", "restore_reminder"),
                keywords=("reminder", "remind", "提醒"),
                max_steps=3,
                instructions=REMINDERS_INSTRUCTIONS,
            ),
            Skill(
                "watches",
                "Monitor websites for changes",
                (
                    "watch this website",
                    "list my watches",
                    "监控这个网页",
                    "取消网页监控",
                    "check this page for changes lah",
                    "can watch this site anot",
                    "把取消的监控恢复",
                    "restore this watch can lah",
                ),
                (
                    "create_website_watch",
                    "list_website_watches",
                    "cancel_website_watch",
                    "restore_website_watch",
                ),
                keywords=("watch", "website", "监控", "网页"),
                max_steps=3,
                instructions=WATCHES_INSTRUCTIONS,
            ),
        ]

    def tool_gates(self):
        return build_gates(self)

    def __init__(
        self,
        settings: JobsSettings,
        access: AccessPolicy,
        platform: ChatPlatform,
        *,
        sender,
        clock=lambda: datetime.now(UTC),
        rendered_fetcher: RenderedFetcher | None = None,
        rendered_allowed: RenderedAllowed | None = None,
    ) -> None:
        self.settings = settings
        self.access = access
        self.platform = platform
        self.sender = sender
        self.clock = clock
        self.rendered_fetcher = rendered_fetcher
        self.rendered_allowed = rendered_allowed
        self.service = JobsService(
            settings,
            access,
            platform,
            clock,
            rendered_allowed=rendered_allowed,
            rendered_available=rendered_fetcher is not None,
        )
        self.worker: JobsWorker | None = None

    def action_handlers(self):
        actions = JobActions(self.service)
        return {"create": actions, "cancel": actions, "configure": actions, "restore": actions}

    @staticmethod
    def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def tools(self) -> list[Tool]:
        async def create_reminder(ctx: ToolContext, **args: Any) -> ToolResult:
            if set(args) != {"text", "local_time", "timezone"} or any(
                not isinstance(v, str) for v in args.values()
            ):
                return ToolResult.error(
                    "Provide reminder text, an ISO local time, and an IANA timezone."
                )
            try:
                action = await self.service.request_create(ctx, JobKind.REMINDER, args)
                return ToolResult(content=action.summary, pending_action=action)
            except (ValueError, PermissionError, RuntimeError) as exc:
                return ToolResult.error(str(exc))

        async def list_reminders(ctx: ToolContext, **args: Any) -> ToolResult:
            if set(args) - {"all_members"}:
                return ToolResult.error("Unknown argument.")
            try:
                return ToolResult(
                    content=await self.service.list_jobs(
                        ctx, JobKind.REMINDER, args.get("all_members", False)
                    )
                )
            except ValueError as exc:
                return ToolResult.error(str(exc))

        async def cancel_reminder(ctx: ToolContext, **args: Any) -> ToolResult:
            return await self._cancel_tool(ctx, JobKind.REMINDER, args)

        async def create_watch(ctx: ToolContext, **args: Any) -> ToolResult:
            if set(args) - {"url", "interval_seconds", "rendered"} or not {
                "url",
                "interval_seconds",
            } <= set(args):
                return ToolResult.error("Provide a website URL and interval in seconds.")
            if "rendered" in args and type(args["rendered"]) is not bool:
                return ToolResult.error("Rendered must be boolean.")
            try:
                action = await self.service.request_create(
                    ctx, JobKind.WATCH, args, url_provenance=True
                )
                return ToolResult(content=action.summary, pending_action=action)
            except (ValueError, PermissionError, RuntimeError) as exc:
                return ToolResult.error(str(exc))

        async def list_watches(ctx: ToolContext, **args: Any) -> ToolResult:
            if set(args) - {"all_members"}:
                return ToolResult.error("Unknown argument.")
            try:
                return ToolResult(
                    content=await self.service.list_jobs(
                        ctx, JobKind.WATCH, args.get("all_members", False)
                    )
                )
            except ValueError as exc:
                return ToolResult.error(str(exc))

        async def cancel_watch(ctx: ToolContext, **args: Any) -> ToolResult:
            return await self._cancel_tool(ctx, JobKind.WATCH, args)

        async def restore_reminder(ctx: ToolContext, **args: Any) -> ToolResult:
            return await self._restore_tool(ctx, JobKind.REMINDER, args)

        async def restore_watch(ctx: ToolContext, **args: Any) -> ToolResult:
            return await self._restore_tool(ctx, JobKind.WATCH, args)

        async def configure(ctx: ToolContext, **args: Any) -> ToolResult:
            keys = {
                "tool",
                "enabled",
                "max_jobs_per_user",
                "max_jobs_per_chat",
                "minimum_interval_seconds",
            }
            if set(args) - keys or not {"tool", "enabled"} <= set(args):
                return ToolResult.error("Provide tool and enabled, with optional quota reductions.")
            kind = self._kind(args["tool"])
            if kind is None or type(args["enabled"]) is not bool:
                return ToolResult.error(
                    "Tool must be reminders or watches and enabled must be boolean."
                )
            try:
                limits = self._numeric_limits(args)
                action = await self.service.request_configure(ctx, kind, args["enabled"], limits)
                return ToolResult(content=action.summary, pending_action=action)
            except (ValueError, PermissionError, RuntimeError) as exc:
                return ToolResult.error(str(exc))

        string = {"type": "string"}
        boolean = {"type": "boolean"}
        integer = {"type": "integer", "minimum": 1}
        return [
            Tool(
                "create_reminder",
                "Create a one-shot reminder; recurring reminders are unsupported. "
                "Applies immediately (may ask the user to confirm after web or file content); "
                "cancel_reminder undoes it.",
                self._object(
                    {
                        "text": {**string, "description": "Reminder message to post."},
                        "local_time": {
                            **string,
                            "description": "Local datetime YYYY-MM-DDTHH:MM[:SS].",
                        },
                        "timezone": {**string, "description": "IANA zone key for local_time."},
                    },
                    ["text", "local_time", "timezone"],
                ),
                create_reminder,
                writes=True,
                direct=True,
            ),
            Tool(
                "list_reminders",
                "List reminders in this conversation and topic",
                self._object(
                    {
                        "all_members": {
                            **boolean,
                            "description": "Include all members; defaults to false.",
                        }
                    },
                    [],
                ),
                list_reminders,
            ),
            Tool(
                "cancel_reminder",
                "Cancel a reminder. Applies immediately (may ask the user to confirm "
                "after web or file content); "
                "restore_reminder undoes it.",
                self._object(
                    {
                        "job_id": {
                            **string,
                            "description": "Reminder ID from list_reminders or the "
                            "cancel/restore echo.",
                        }
                    },
                    ["job_id"],
                ),
                cancel_reminder,
                writes=True,
                direct=True,
            ),
            Tool(
                "create_website_watch",
                "Watch a public site's readable text; not for one-off reads → fetch_web_page. "
                "Use rendered for JS-heavy sites, interval at least the group minimum. "
                "Applies immediately (may ask the user to confirm after web or file content); "
                "cancel_website_watch undoes it.",
                self._object(
                    {
                        "url": {**string, "description": "Posted or search-returned HTTP(S) URL."},
                        "interval_seconds": {
                            **integer,
                            "description": "Polling seconds; at least the group minimum.",
                        },
                        "rendered": {
                            **boolean,
                            "description": "Render JS-heavy sites; defaults to false.",
                        },
                    },
                    ["url", "interval_seconds"],
                ),
                create_watch,
                writes=True,
                direct=True,
            ),
            Tool(
                "list_website_watches",
                "List website watches in this conversation and topic",
                self._object(
                    {
                        "all_members": {
                            **boolean,
                            "description": "Include all members; defaults to false.",
                        }
                    },
                    [],
                ),
                list_watches,
            ),
            Tool(
                "cancel_website_watch",
                "Cancel a website watch. Applies immediately (may ask the user to confirm "
                "after web or file content); "
                "restore_website_watch undoes it.",
                self._object(
                    {
                        "job_id": {
                            **string,
                            "description": "Website watch ID from list_website_watches or "
                            "the cancel/restore echo.",
                        }
                    },
                    ["job_id"],
                ),
                cancel_watch,
                writes=True,
                direct=True,
            ),
            Tool(
                "restore_reminder",
                "Restore a cancelled reminder. Applies immediately (may ask the user to confirm "
                "after web or file content); cancel_reminder undoes it.",
                self._object(
                    {
                        "job_id": {
                            **string,
                            "description": "Reminder ID from list_reminders or the "
                            "cancel/restore echo.",
                        }
                    },
                    ["job_id"],
                ),
                restore_reminder,
                writes=True,
                direct=True,
            ),
            Tool(
                "restore_website_watch",
                "Restore a cancelled website watch. Applies immediately (may ask the user "
                "to confirm "
                "after web or file content); cancel_website_watch undoes it.",
                self._object(
                    {
                        "job_id": {
                            **string,
                            "description": "Website watch ID from list_website_watches or "
                            "the cancel/restore echo.",
                        }
                    },
                    ["job_id"],
                ),
                restore_watch,
                writes=True,
                direct=True,
            ),
            Tool(
                "configure_jobs",
                "Request reminders or watches on/off for this group. A current setup manager, "
                "group creator or administrator may request; requester confirms.",
                self._object(
                    {
                        "tool": {
                            "description": "Feature: reminders or watches.",
                            "type": "string",
                            "enum": ["reminders", "watches"],
                        },
                        "enabled": {**boolean, "description": "Whether to turn the feature on."},
                        "max_jobs_per_user": {**integer, "description": "Maximum jobs per user."},
                        "max_jobs_per_chat": {
                            **integer,
                            "description": "Maximum jobs in this group.",
                        },
                        "minimum_interval_seconds": {
                            **integer,
                            "description": "Minimum polling interval in seconds.",
                        },
                    },
                    ["tool", "enabled"],
                ),
                configure,
                writes=True,
                admin_only=False,
            ),
        ]

    async def _cancel_tool(
        self, ctx: ToolContext, kind: JobKind, args: dict[str, Any]
    ) -> ToolResult:
        if set(args) != {"job_id"}:
            return ToolResult.error("Provide a job ID.")
        try:
            action = await self.service.request_cancel(ctx, kind, args["job_id"])
        except ValueError as exc:
            return ToolResult.error(str(exc))
        if action is None:
            return ToolResult.error("No matching active job was found.")
        return ToolResult(content=action.summary, pending_action=action)

    async def _restore_tool(
        self, ctx: ToolContext, kind: JobKind, args: dict[str, Any]
    ) -> ToolResult:
        if set(args) != {"job_id"}:
            return ToolResult.error("Provide a job ID.")
        try:
            action = await self.service.request_restore(ctx, kind, args["job_id"])
        except (ValueError, PermissionError, RuntimeError) as exc:
            return ToolResult.error(str(exc))
        if action is None:
            return ToolResult.error("No matching cancelled job was found.")
        return ToolResult(content=action.summary, pending_action=action)

    @staticmethod
    def _kind(value: object) -> JobKind | None:
        if value == "reminders":
            return JobKind.REMINDER
        if value == "watches":
            return JobKind.WATCH
        return None

    @staticmethod
    def _numeric_limits(values: dict[str, Any]) -> dict[str, int]:
        result = {}
        for key in ("max_jobs_per_user", "max_jobs_per_chat", "minimum_interval_seconds"):
            if key in values:
                if type(values[key]) is not int:
                    raise ValueError(f"{key} must be an integer")
                result[key] = values[key]
        return result

    async def _confirm_command(self, ctx: CommandContext, action: PendingAction) -> str | None:
        if ctx.confirm is None:
            return "Confirmation is unavailable; nothing changed."
        await ctx.confirm.request(
            action, ctx.user, reply_to=ctx.message.ref if ctx.message else None
        )
        return None

    def commands(self) -> list[Command]:
        async def remind(ctx: CommandContext, args: str) -> str | None:
            parts = args.strip().split(maxsplit=2)
            if len(parts) != 3:
                return "Use /remind <ISO local time> <IANA timezone> <text>."
            try:
                action = await self.service.request_create(
                    ctx,
                    JobKind.REMINDER,
                    {"local_time": parts[0], "timezone": parts[1], "text": parts[2]},
                )
                return await self._confirm_command(ctx, action)
            except (ValueError, PermissionError, RuntimeError) as exc:
                return str(exc)

        async def reminders(ctx: CommandContext, args: str) -> str:
            if args.strip() not in {"", "all"}:
                return "Use /reminders [all]."
            return await self.service.list_jobs(ctx, JobKind.REMINDER, args.strip() == "all")

        async def cancel_reminder(ctx: CommandContext, args: str) -> str | CommandReply | None:
            return await self._command_cancel(ctx, JobKind.REMINDER, args)

        async def watch(ctx: CommandContext, args: str) -> str | None:
            parts = args.strip().split(maxsplit=1)
            if len(parts) != 2 or not parts[0].isdecimal():
                return "Use /watch <seconds> <public URL>."
            try:
                action = await self.service.request_create(
                    ctx, JobKind.WATCH, {"interval_seconds": int(parts[0]), "url": parts[1]}
                )
                return await self._confirm_command(ctx, action)
            except (ValueError, PermissionError, RuntimeError) as exc:
                return str(exc)

        async def watch_rendered(ctx: CommandContext, args: str) -> str | None:
            parts = args.strip().split(maxsplit=1)
            if len(parts) != 2 or not parts[0].isdecimal():
                return "Use /watchrendered <seconds> <public URL>."
            try:
                action = await self.service.request_create(
                    ctx,
                    JobKind.WATCH,
                    {"interval_seconds": int(parts[0]), "url": parts[1], "rendered": True},
                )
                return await self._confirm_command(ctx, action)
            except (ValueError, PermissionError, RuntimeError) as exc:
                return str(exc)

        async def watches(ctx: CommandContext, args: str) -> str:
            if args.strip() not in {"", "all"}:
                return "Use /watches [all]."
            return await self.service.list_jobs(ctx, JobKind.WATCH, args.strip() == "all")

        async def cancel_watch(ctx: CommandContext, args: str) -> str | CommandReply | None:
            return await self._command_cancel(ctx, JobKind.WATCH, args)

        async def jobtools(ctx: CommandContext, args: str) -> str | CommandReply | None:
            parts = args.split()
            if (
                len(parts) < 2
                or parts[0] not in {"reminders", "watches"}
                or parts[1] not in {"on", "off"}
            ):
                return CommandReply(
                    "Reply to this message with what to change, e.g. reminders off or "
                    "watches on. Limits can follow: max_jobs_per_user=5.",
                    awaits_reply=True,
                    placeholder="e.g. reminders off",
                )
            limits: dict[str, int] = {}
            for part in parts[2:]:
                key, sep, value = part.partition("=")
                if (
                    not sep
                    or key
                    not in {"max_jobs_per_user", "max_jobs_per_chat", "minimum_interval_seconds"}
                    or not value.isdecimal()
                    or key in limits
                ):
                    return (
                        "Use unique limit=value entries for max_jobs_per_user, "
                        "max_jobs_per_chat, or minimum_interval_seconds."
                    )
                limits[key] = int(value)
            try:
                action = await self.service.request_configure(
                    ctx, self._kind(parts[0]), parts[1] == "on", limits
                )
                return await self._confirm_command(ctx, action)
            except (ValueError, PermissionError, RuntimeError) as exc:
                return str(exc)

        return [
            Command("remind", "Create a confirmed one-shot reminder", remind),
            Command("reminders", "List reminders; admins may use all", reminders),
            Command("cancelreminder", "Request reminder cancellation", cancel_reminder),
            Command("watch", "Watch a public website's readable text", watch),
            Command("watchrendered", "Watch a JavaScript website's readable text", watch_rendered),
            Command("watches", "List website watches; admins may use all", watches),
            Command("cancelwatch", "Request website watch cancellation", cancel_watch),
            Command("jobtools", "Request group job settings changes", jobtools),
        ]

    async def _command_cancel(
        self, ctx: CommandContext, kind: JobKind, args: str
    ) -> str | CommandReply | None:
        if not args.strip():
            noun = "reminder" if kind is JobKind.REMINDER else "watch"
            try:
                listing = await self.service.list_jobs(ctx, kind, False)
            except (ValueError, PermissionError, RuntimeError) as exc:
                return str(exc)
            if listing == "No jobs found.":
                return (
                    f"You have no active {noun}es to cancel."
                    if noun == "watch"
                    else (f"You have no active {noun}s to cancel.")
                )
            return CommandReply(
                f"{listing}\nReply to this message with the ID of the {noun} to cancel.",
                awaits_reply=True,
                placeholder=f"{noun.capitalize()} ID",
            )
        try:
            action = await self.service.request_cancel(ctx, kind, args.strip())
        except ValueError as exc:
            return str(exc)
        if action is None:
            return "No matching active job was found."
        return await self._confirm_command(ctx, action)

    async def on_startup(self, stores: Stores) -> None:
        self._stores = stores
        if stores.db is None:
            return
        if self.worker is None:
            repo = JobsRepo(stores.db, self.settings)

            self.worker = JobsWorker(
                repo,
                self.settings,
                authorize=lambda job: self._authorize_job(job, repo),
                sender=self.sender,
                refresh_access=self.access.refresh,
                clock=self.clock,
                rendered_fetcher=self.rendered_fetcher,
            )
        await self.worker.start()

    async def _authorize_job(self, job: Job, repo: JobsRepo) -> bool:
        scope = job.scope
        if not await can_use_scope(
            scope,
            job.owner,
            access=self.access,
            platform=self.platform,
            dm_enabled=self.settings.dm_enabled,
        ):
            return False
        config = await repo.settings(scope)
        if not config["reminders_enabled" if job.kind is JobKind.REMINDER else "watches_enabled"]:
            return False
        if job.kind is JobKind.WATCH:
            rendered = job.payload.get("mode", "plain") == "rendered"
            if job.payload.get("mode", "plain") not in {"plain", "rendered"}:
                return False
            if rendered and (
                self.rendered_fetcher is None
                or self.rendered_allowed is None
                or self.settings.max_rendered_watches_global == 0
                or not await self.rendered_allowed(scope)
            ):
                return False
            interval = job.payload.get("interval_seconds")
            rendered_floor = max(
                config["minimum_interval_seconds"],
                self.settings.minimum_watch_interval_seconds,
                self.settings.minimum_rendered_watch_interval_seconds if rendered else 0,
            )
            return (
                type(interval) is int
                and (not rendered or rendered_floor <= self.settings.maximum_watch_interval_seconds)
                and rendered_floor <= interval <= self.settings.maximum_watch_interval_seconds
            )
        return True

    async def aclose(self) -> None:
        if self.worker is not None:
            await self.worker.aclose()

    async def purge_user(self, chat: ChatRef, user: UserRef, stores: Stores) -> None:
        if stores.db is not None:
            await JobsRepo(stores.db, self.settings).purge_user(chat.key, user.key)

    async def purge_chat(self, chat: ChatRef, stores: Stores) -> None:
        if stores.db is not None:
            await JobsRepo(stores.db, self.settings).purge_chat(chat.key)

    async def purge_older_than(self, cutoff: datetime, stores: Stores) -> None:
        if stores.db is not None:
            await JobsRepo(stores.db, self.settings).purge_older_than(cutoff)
