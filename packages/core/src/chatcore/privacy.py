"""Privacy disclosure and deletion commands for a configured bot."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from chatcore.administration.store import is_global_scope
from chatcore.events import ChatRef, UserRef
from chatcore.pending import PendingAction, PendingActionHandler
from chatcore.platform import ChatPlatform
from chatcore.plugin import BasePlugin, Command, CommandContext, Plugin
from chatcore.store.base import Stores

log = logging.getLogger(__name__)

DEFAULT_NOTICE = (
    "This bot records messages in this group to answer questions about past chat. Messages are "
    "kept 12 months. When someone asks the bot something, recent messages are sent to the AI "
    "provider ({provider}). /privacy for details, /forgetme to delete yours."
)
MemberGroups = Callable[[UserRef], Awaitable[list[ChatRef]]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _safe_chat_target(value: object) -> ChatRef | None:
    if isinstance(value, ChatRef):
        target = value
    elif isinstance(value, str):
        try:
            target = ChatRef.parse(value)
        except ValueError:
            return None
    else:
        return None
    if not target.platform or not target.id or is_global_scope(target) or ":" in target.id:
        return None
    return target


class _PrivacyActions:
    def __init__(self, plugin: PrivacyPlugin) -> None:
        self.plugin = plugin

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        if action.requester != ctx.user.ref:
            return "Only the person who requested this deletion can confirm it."
        if action.kind == "erasure_release" and not ctx.is_admin:
            return "Only an administrator can release memory writes."
        if action.kind == "forgetme":
            return await self.plugin._forgetme(action, ctx)
        if action.kind == "purgechat":
            return await self.plugin._purgechat(action, ctx)
        if action.kind == "erasure_release":
            return await self.plugin._release_erasure(action, ctx)
        return "Unknown privacy action."


class PrivacyPlugin(BasePlugin):
    name = "privacy"
    help_title = "Privacy and your data"

    def __init__(
        self,
        plugins: Sequence[Plugin],
        *,
        provider_label: str,
        images_notice: str | None = None,
        member_groups: MemberGroups | None = None,
        notice: str = DEFAULT_NOTICE,
        retention_days: int = 365,
        timezone: str = "Asia/Singapore",
        run_at: time = time(3, 15),
        clock: Clock | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self.plugins = [plugin for plugin in plugins if plugin is not self]
        self.provider_label = provider_label
        self.images_notice = images_notice
        self.member_groups = member_groups
        self.notice = notice
        self.retention_days = retention_days
        self.timezone = ZoneInfo(timezone)
        self.run_at = run_at
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or asyncio.sleep
        self._retention_task: asyncio.Task | None = None
        self._erasure_task: asyncio.Task | None = None

    def action_handlers(self) -> dict[str, PendingActionHandler]:
        actions = _PrivacyActions(self)
        return {"forgetme": actions, "purgechat": actions, "erasure_release": actions}

    def commands(self) -> list[Command]:
        async def privacy(ctx: CommandContext, _args: str) -> str:
            result = (
                self.notice.format(provider=self.provider_label)
                + "\n\nThe expense ledger is kept so balances add up; memories remain until "
                "/forget. Messages and usage logs are removed after 12 months. "
                "/forgetme deletes your messages, related conversation data, and memories about "
                "you or supported only by your messages, including your personal display time "
                "zone. Independently supported shared facts remain without your source links; "
                "whole-word name matches are reported for manual review. Shared group settings "
                "stay. /forget deletes "
                "a memory. DMs are separate and are never readable from the group."
            )
            if self.images_notice:
                result += "\n\nImages: " + self.images_notice
            return result

        async def forgetme(ctx: CommandContext, _args: str) -> None:
            chats = [ctx.chat]
            if ctx.is_dm and self.member_groups:
                chats.extend(await self.member_groups(ctx.user.ref))
            chats = list(dict((chat.key, chat) for chat in chats).values())
            keys = [chat.key for chat in chats]
            summary = (
                f"Delete {ctx.user.display_name}'s messages, conversation turns that quote them, "
                f"memories supported by their messages or resolved identity, and personal "
                f"display time-zone preferences in: "
                f"{', '.join(keys)}. Shared group display settings and expense records stay so "
                "balances still add up. Independently supported shared facts may remain without "
                "their source links; name matches are reported for review, not deleted by name."
            )
            action = PendingAction.create(
                chat=ctx.chat,
                requester=ctx.user.ref,
                plugin=self.name,
                kind="forgetme",
                payload={"chats": keys},
                summary=summary,
                thread_id=ctx.thread_id,
            )
            if ctx.confirm:
                await ctx.confirm.request(
                    action, ctx.user, reply_to=ctx.message.ref if ctx.message else None
                )
            return None

        async def purgechat(ctx: CommandContext, args: str) -> None:
            target = _safe_chat_target(args.strip() if args.strip() else ctx.chat)
            if target is None:
                return "Use /purgechat or /purgechat <platform:chat_id>."
            action = PendingAction.create(
                chat=ctx.chat,
                requester=ctx.user.ref,
                plugin=self.name,
                kind="purgechat",
                payload={"chat": target.key},
                summary=f"Delete everything for {target.key}, including messages, settings, "
                "members and the ledger.",
                thread_id=ctx.thread_id,
            )
            if ctx.confirm:
                await ctx.confirm.request(
                    action, ctx.user, reply_to=ctx.message.ref if ctx.message else None
                )
            return None

        async def erasures(ctx: CommandContext, args: str) -> str:
            rows = await ctx.stores.repo.list_erasures(None if ctx.is_dm else ctx.chat)
            parts = args.strip().split()
            if parts:
                if len(parts) != 2 or parts[0] not in {"retry", "release"}:
                    return (
                        "Use /erasures, /erasures retry <operation-id>, or "
                        "/erasures release <operation-id>."
                    )
                operation = next((row for row in rows if row["operation_id"] == parts[1]), None)
                if operation is None:
                    return "No outstanding erasure with that ID in this scope."
                if parts[0] == "release":
                    action = PendingAction.create(
                        chat=ctx.chat,
                        requester=ctx.user.ref,
                        plugin=self.name,
                        kind="erasure_release",
                        payload={"operation_id": parts[1]},
                        summary=(
                            f"Release paused memory writes for erasure {parts[1]} and retry "
                            "remaining deletion steps."
                        ),
                        thread_id=ctx.thread_id,
                    )
                    if ctx.confirm:
                        await ctx.confirm.request(
                            action, ctx.user, reply_to=ctx.message.ref if ctx.message else None
                        )
                    return ""
                done, _ = await self._process_erasure(operation, ctx.stores, force=True)
                return (
                    "Erasure completed." if done else "Erasure remains incomplete and will retry."
                )
            if not rows:
                result = "No outstanding erasures."
            else:
                result = "\n".join(
                    f"{row['operation_id']} {row['scope']} {row['status']} "
                    f"attempts={row['attempts']}"
                    for row in rows
                )
            pending = await self._pending_cleanup(ctx.chat, ctx.stores)
            if pending:
                result += f"\nSearch cleanup: {pending} pending item(s)."
            return result

        return [
            Command("privacy", "Show data and privacy details", privacy),
            Command("forgetme", "Request deletion of your personal chat data", forgetme),
            Command("purgechat", "Delete all data for a chat", purgechat, admin_only=True),
            Command("erasures", "List or retry privacy erasures", erasures, admin_only=True),
        ]

    async def on_bot_added(self, chat: ChatRef, platform: ChatPlatform, stores: Stores) -> None:
        key = f"joined:{chat.key}"
        async with stores.cache.lock(key, ttl_s=60):
            if await stores.cache.get(key) is not None:
                return
            ref = await platform.send(chat, self.notice.format(provider=self.provider_label))
            if platform.capabilities.pin:
                try:
                    await platform.pin(ref)
                except Exception as exc:
                    log.warning(
                        "privacy notice pin failed chat=%s error=%s", chat.key, type(exc).__name__
                    )
            await stores.cache.set(key, b"1", ttl_s=600)

    async def _run_purges(self, method: str, stores: Stores, *args) -> tuple[int, bool]:
        """Compatibility helper for plugin integrations; erasure journal owns production purges."""
        failed = False
        for plugin in sorted(
            self.plugins,
            key=lambda item: bool(getattr(item, "purge_reads_messages", False)),
        ):
            hook = getattr(plugin, method, None)
            if not callable(hook):
                continue
            try:
                await hook(*args, stores)
            except Exception as exc:
                failed = True
                log.error("privacy plugin purge failed error=%s", type(exc).__name__)
        return 0, failed

    async def _forgetme(self, action: PendingAction, ctx: CommandContext) -> str:
        total = 0
        notes: list[str] = []
        for key in action.payload.get("chats", []):
            chat = _safe_chat_target(key)
            if chat is None:
                notes.append("An invalid chat target was skipped.")
                continue
            operation_id = secrets.token_hex(12)
            try:
                operation = await ctx.stores.repo.create_erasure(
                    operation_id, chat, "user", ctx.user.ref.key
                )
            except Exception as exc:
                log.error("privacy erasure=%s error=%s", operation_id, type(exc).__name__)
                try:
                    operation = next(
                        (
                            row
                            for row in await ctx.stores.repo.list_erasures(chat)
                            if row["operation_id"] == operation_id
                        ),
                        None,
                    )
                except Exception as lookup_exc:
                    operation = None
                    log.error(
                        "privacy erasure=%s error=%s", operation_id, type(lookup_exc).__name__
                    )
                if operation is None:
                    notes.append(f"Deletion could not start for {chat.key}; please try again.")
                    continue
            try:
                done, count = await self._run_erasure(operation, ctx.stores)
            except Exception as exc:
                log.error("privacy erasure=%s error=%s", operation_id, type(exc).__name__)
                done, count = False, 0
            total += count
            if not done:
                notes.append("Deletion is incomplete and will be retried automatically.")
            for plugin in self.plugins:
                hook = getattr(plugin, "erasure_notes", None)
                if callable(hook):
                    try:
                        note = await hook(chat, ctx.user.ref, ctx.stores)
                    except Exception as exc:
                        log.error(
                            "privacy plugin %s erasure_notes failed: %s",
                            plugin.name,
                            type(exc).__name__,
                        )
                    else:
                        if note and note not in notes:
                            notes.append(note)
            pending = await self._pending_cleanup(chat, ctx.stores)
            if pending:
                notes.append(f"Search cleanup has {pending} pending item(s).")
        result = (
            f"Deleted {total} stored message and pending-action rows. Expense records stay so "
            "balances still add up; your member name stays for the ledger, your personal display "
            "time-zone preferences are deleted, and shared chat settings stay."
        )
        if notes:
            result += " " + " ".join(notes)
        return result

    async def _purgechat(self, action: PendingAction, ctx: CommandContext) -> str:
        chat = _safe_chat_target(action.payload.get("chat"))
        if chat is None:
            return "That chat target is invalid. Nothing was deleted."
        operation_id = secrets.token_hex(12)
        try:
            operation = await ctx.stores.repo.create_erasure(operation_id, chat, "chat", None)
        except Exception as exc:
            log.error("privacy erasure=%s error=%s", operation_id, type(exc).__name__)
            try:
                operation = next(
                    (
                        row
                        for row in await ctx.stores.repo.list_erasures(chat)
                        if row["operation_id"] == operation_id
                    ),
                    None,
                )
            except Exception as lookup_exc:
                operation = None
                log.error("privacy erasure=%s error=%s", operation_id, type(lookup_exc).__name__)
            if operation is None:
                return f"Deletion could not start for {chat.key}; please try again."
        try:
            done, count = await self._run_erasure(operation, ctx.stores)
        except Exception as exc:
            log.error("privacy erasure=%s error=%s", operation_id, type(exc).__name__)
            done, count = False, 0
        result = f"Deleted {count} core rows for {chat.key}."
        pending = await self._pending_cleanup(chat, ctx.stores)
        if pending:
            result += f" Search cleanup has {pending} pending item(s)."
        return (
            result
            if done
            else result + " Deletion is incomplete and will be retried automatically."
        )

    async def _pending_cleanup(self, chat: ChatRef, stores: Stores) -> int:
        count = 0
        for plugin in self.plugins:
            hook = getattr(plugin, "pending_cleanup", None)
            if callable(hook):
                try:
                    count += int(await hook(chat, stores))
                except Exception as exc:
                    log.error(
                        "privacy plugin %s pending_cleanup failed: %s",
                        plugin.name,
                        type(exc).__name__,
                    )
        return count

    async def _run_erasure(self, operation, stores: Stores):
        """Run a newly created erasure, first running older queued ones in order.

        A new operation is queued behind any active or queued one. When nothing is active,
        promote and run the oldest queued operations one at a time until this one runs.
        If another operation stays active (running elsewhere or failed), this one stays
        queued for recovery.
        """
        chat = ChatRef.parse(operation["chat_key"])
        operation_id = operation["operation_id"]
        while True:
            current = next(
                (
                    row
                    for row in await stores.repo.list_erasures(chat)
                    if row["operation_id"] == operation_id
                ),
                None,
            )
            if current is None:
                return False, 0
            if current["status"] == "active":
                return await self._process_erasure(current, stores, force=True)
            async with stores.cache.lock(f"erasure:{chat.key}", ttl_s=120):
                promoted = await stores.repo.promote_erasure(chat)
            if promoted is None:
                return False, 0
            if promoted["operation_id"] == operation_id:
                return await self._process_erasure(promoted, stores, force=True)
            done, _ = await self._process_erasure(promoted, stores, force=True, chain=False)
            if not done:
                return False, 0

    async def _process_erasure(
        self, operation, stores: Stores, *, force: bool = False, chain: bool = True
    ):
        chat = ChatRef.parse(operation["chat_key"])
        promoted = None
        async with stores.cache.lock(f"erasure:{chat.key}", ttl_s=120):
            current = next(
                (
                    row
                    for row in await stores.repo.list_erasures(chat)
                    if row["operation_id"] == operation["operation_id"]
                ),
                None,
            )
            if current is None or current["status"] != "active":
                return False, 0
            if not force and current["next_attempt_at"] > self.clock():
                return False, 0
            completed = set(current["completed_steps"])
            target = (
                UserRef.parse(current["target_user_key"]) if current["target_user_key"] else None
            )
            method = "purge_user" if target else "purge_chat"
            total = 0
            result = None
            errors: list[str] = []
            ordered = sorted(
                self.plugins,
                key=lambda plugin: bool(getattr(plugin, "purge_reads_messages", False)),
            )
            for plugin in self.plugins:
                step = f"begin:{plugin.name}"
                if step in completed:
                    continue
                hook = getattr(plugin, "begin_erasure", None)
                try:
                    if callable(hook):
                        await hook(chat, current["operation_id"], current["scope"], target, stores)
                    completed.add(step)
                    await stores.repo.update_erasure(
                        current["operation_id"], completed_steps=sorted(completed)
                    )
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    log.error(
                        "privacy erasure=%s error=%s", current["operation_id"], type(exc).__name__
                    )
            if errors:
                attempts = int(current["attempts"]) + 1
                delay = min(3600, 300 * 2 ** min(attempts - 1, 4))
                await stores.repo.update_erasure(
                    current["operation_id"],
                    attempts=attempts,
                    next_attempt_at=self.clock() + timedelta(seconds=delay),
                    last_error_code=errors[0],
                )
                return False, 0
            for plugin in ordered:
                step = f"plugin:{plugin.name}"
                if step in completed:
                    continue
                hook = getattr(plugin, method, None)
                try:
                    if callable(hook):
                        purged = (
                            await hook(chat, target, stores) if target else await hook(chat, stores)
                        )
                        total += purged if isinstance(purged, int) else 0
                    completed.add(step)
                    await stores.repo.update_erasure(
                        current["operation_id"], completed_steps=sorted(completed)
                    )
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    log.error(
                        "privacy erasure=%s error=%s", current["operation_id"], type(exc).__name__
                    )
            reader_steps = {
                f"plugin:{plugin.name}"
                for plugin in ordered
                if getattr(plugin, "purge_reads_messages", False)
            }
            if "core" not in completed and reader_steps <= completed:
                try:
                    total += (
                        await stores.repo.purge_user(chat, target)
                        if target
                        else await stores.repo.purge_chat(chat)
                    )
                    completed.add("core")
                    await stores.repo.update_erasure(
                        current["operation_id"], completed_steps=sorted(completed)
                    )
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    log.error(
                        "privacy erasure=%s error=%s", current["operation_id"], type(exc).__name__
                    )
            required = {f"plugin:{plugin.name}" for plugin in self.plugins} | {"core"}
            if required <= completed and not errors:
                for plugin in self.plugins:
                    hook = getattr(plugin, "finish_erasure", None)
                    if callable(hook):
                        try:
                            await hook(chat, current["operation_id"], success=True, stores=stores)
                        except Exception as exc:
                            errors.append(type(exc).__name__)
                if not errors:
                    await stores.repo.delete_erasure(current["operation_id"])
                    result = (True, total)
                    try:
                        promoted = await stores.repo.promote_erasure(chat)
                    except Exception as exc:
                        log.error(
                            "privacy erasure=%s error=%s",
                            current["operation_id"],
                            type(exc).__name__,
                        )
            if result is None:
                for plugin in self.plugins:
                    hook = getattr(plugin, "finish_erasure", None)
                    if callable(hook):
                        try:
                            await hook(chat, current["operation_id"], success=False, stores=stores)
                        except Exception as exc:
                            log.error(
                                "privacy erasure=%s error=%s",
                                current["operation_id"],
                                type(exc).__name__,
                            )
                attempts = int(current["attempts"]) + 1
                delay = min(3600, 300 * 2 ** min(attempts - 1, 4))
                await stores.repo.update_erasure(
                    current["operation_id"],
                    attempts=attempts,
                    next_attempt_at=self.clock() + timedelta(seconds=delay),
                    last_error_code=errors[0] if errors else None,
                )
                result = (False, total)
        if promoted is not None and chain:
            try:
                await self._process_erasure(promoted, stores, force=True)
            except Exception as exc:
                log.error(
                    "privacy erasure=%s error=%s",
                    promoted["operation_id"],
                    type(exc).__name__,
                )
        return result

    async def recover_erasures(self, stores: Stores) -> None:
        rows = await stores.repo.list_erasures()
        chats = sorted({row["chat_key"] for row in rows})
        for chat_key in chats:
            chat = ChatRef.parse(chat_key)
            active = None
            operation_id = next(
                (row["operation_id"] for row in rows if row["chat_key"] == chat_key), "unknown"
            )
            try:
                active = next(
                    (
                        row
                        for row in rows
                        if row["chat_key"] == chat_key and row["status"] == "active"
                    ),
                    None,
                )
                if active is None:
                    async with stores.cache.lock(f"erasure:{chat.key}", ttl_s=120):
                        active = await stores.repo.promote_erasure(chat)
                if active is not None and (
                    active["status"] == "active" and active["next_attempt_at"] <= self.clock()
                ):
                    await self._process_erasure(active, stores)
            except Exception as exc:
                operation_id = active["operation_id"] if active else operation_id
                log.error("privacy erasure=%s error=%s", operation_id, type(exc).__name__)

    async def _release_erasure(self, action: PendingAction, ctx: CommandContext) -> str:
        operation_id = action.payload.get("operation_id")
        rows = await ctx.stores.repo.list_erasures(None if ctx.is_dm else ctx.chat)
        operation = next((row for row in rows if row["operation_id"] == operation_id), None)
        if operation is None:
            return "No outstanding erasure with that ID in this scope."
        completed = set(operation["completed_steps"])
        blocking = [
            plugin for plugin in self.plugins if getattr(plugin, "erasure_blocks_writes", False)
        ]
        if any(f"plugin:{plugin.name}" not in completed for plugin in blocking):
            return "Memory writes cannot be released until its deletion step has completed."
        for plugin in blocking:
            hook = getattr(plugin, "release_erasure", None)
            if callable(hook):
                await hook(ChatRef.parse(operation["chat_key"]), operation_id, ctx.stores)
        await ctx.stores.repo.update_erasure(
            operation_id, next_attempt_at=self.clock(), last_error_code=None
        )
        done, _ = await self._process_erasure(operation, ctx.stores, force=True)
        return (
            "Remaining erasure steps completed."
            if done
            else "Erasure remains incomplete and will retry."
        )

    async def run_retention(self, stores: Stores, *, now: datetime | None = None) -> dict[str, int]:
        now = now or self.clock()
        local_date = now.astimezone(self.timezone).date().isoformat()
        async with stores.cache.lock("retention", ttl_s=3600):
            if await stores.cache.get(f"retention:{local_date}") is not None:
                return {}
            return await self._run_retention_once(stores, now, local_date)

    async def _run_retention_once(
        self, stores: Stores, now: datetime, local_date: str
    ) -> dict[str, int]:
        cutoff = now - timedelta(days=self.retention_days)
        counts: dict[str, int] = {}
        failed = False
        for plugin in self.plugins:
            hook = getattr(plugin, "purge_older_than", None)
            if not callable(hook):
                continue
            try:
                await hook(cutoff, stores)
                counts[plugin.name] = 0
                log.info("retention plugin=%s deleted=%d", plugin.name, 0)
            except Exception as exc:
                failed = True
                counts[plugin.name] = -1
                log.error("retention plugin=%s failed: %s", plugin.name, type(exc).__name__)
        try:
            counts["core"] = await stores.repo.purge_older_than(cutoff, now=now)
            log.info("retention core deleted=%d", counts["core"])
        except Exception as exc:
            failed = True
            counts["core"] = -1
            log.error("retention core failed: %s", type(exc).__name__)
        if not failed:
            await stores.cache.set(f"retention:{local_date}", b"1", ttl_s=172800)
        return counts

    async def on_startup(self, stores: Stores) -> None:
        try:
            await self.recover_erasures(stores)
        except Exception as exc:
            log.error("privacy erasure recovery failed error=%s", type(exc).__name__)
        if self._erasure_task is None or self._erasure_task.done():
            self._erasure_task = asyncio.create_task(self._erasure_loop(stores))
        if self._retention_task is None or self._retention_task.done():
            self._retention_task = asyncio.create_task(self._retention_loop(stores))

    async def _erasure_loop(self, stores: Stores) -> None:
        delay = 300
        while True:
            await self.sleep(delay)
            try:
                await self.recover_erasures(stores)
                delay = 300
            except Exception as exc:
                log.error("privacy erasure recovery failed error=%s", type(exc).__name__)
                delay = min(3600, delay * 2)

    async def _retention_loop(self, stores: Stores) -> None:
        while True:
            now = self.clock().astimezone(self.timezone)
            target = datetime.combine(now.date(), self.run_at, self.timezone)
            if target <= now:
                target += timedelta(days=1)
            await self.sleep(max((target - now).total_seconds(), 0))
            local_date = self.clock().astimezone(self.timezone).date().isoformat()
            try:
                if await stores.cache.get(f"retention:{local_date}") is None:
                    await self.run_retention(stores)
            except Exception as exc:
                log.error("retention run failed: %s", type(exc).__name__)
