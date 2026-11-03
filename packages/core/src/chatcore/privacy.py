"""Privacy disclosure and deletion commands for a configured bot."""

from __future__ import annotations

import asyncio
import logging
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
        if action.kind == "forgetme":
            return await self.plugin._forgetme(action, ctx)
        if action.kind == "purgechat":
            return await self.plugin._purgechat(action, ctx)
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

    def action_handlers(self) -> dict[str, PendingActionHandler]:
        actions = _PrivacyActions(self)
        return {"forgetme": actions, "purgechat": actions}

    def commands(self) -> list[Command]:
        async def privacy(ctx: CommandContext, _args: str) -> str:
            result = (
                self.notice.format(provider=self.provider_label)
                + "\n\nThe expense ledger is kept so balances add up; memories remain until "
                "/forget. Messages and usage logs are removed after 12 months. "
                "/forgetme deletes your messages, related conversation data, and memories about "
                "you, including your personal display time zone. Shared group display settings "
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
                f"memories about them, and personal display time-zone preferences in: "
                f"{', '.join(keys)}. Shared group display settings and expense records stay so "
                "balances still add up."
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

        return [
            Command("privacy", "Show data and privacy details", privacy),
            Command("forgetme", "Request deletion of your personal chat data", forgetme),
            Command("purgechat", "Delete all data for a chat", purgechat, admin_only=True),
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
        errors = False
        for plugin in self.plugins:
            hook = getattr(plugin, method, None)
            if not callable(hook):
                continue
            try:
                await hook(*args, stores)
            except Exception as exc:
                errors = True
                log.error(
                    "privacy plugin %s %s failed: %s", plugin.name, method, type(exc).__name__
                )
        return 0, errors

    async def _forgetme(self, action: PendingAction, ctx: CommandContext) -> str:
        total = 0
        partial = False
        for key in action.payload.get("chats", []):
            chat = _safe_chat_target(key)
            if chat is None:
                partial = True
                continue
            _, failed = await self._run_purges("purge_user", ctx.stores, chat, ctx.user.ref)
            partial |= failed
            try:
                total += await ctx.stores.repo.purge_user(chat, ctx.user.ref)
            except Exception as exc:
                partial = True
                log.error(
                    "privacy core purge_user failed chat=%s error=%s", chat.key, type(exc).__name__
                )
        result = (
            f"Deleted {total} stored message and pending-action rows. Expense records stay so "
            "balances still add up; your member name stays for the ledger, your personal display "
            "time-zone preferences are deleted, and shared chat settings stay."
        )
        return (
            result + " Some data could not be deleted; an administrator should review logs."
            if partial
            else result
        )

    async def _purgechat(self, action: PendingAction, ctx: CommandContext) -> str:
        chat = _safe_chat_target(action.payload.get("chat"))
        if chat is None:
            return "That chat target is invalid. Nothing was deleted."
        _, partial = await self._run_purges("purge_chat", ctx.stores, chat)
        try:
            count = await ctx.stores.repo.purge_chat(chat)
        except Exception as exc:
            count, partial = 0, True
            log.error(
                "privacy core purge_chat failed chat=%s error=%s", chat.key, type(exc).__name__
            )
        result = f"Deleted {count} core rows for {chat.key}."
        return (
            result + " Some data could not be deleted; an administrator should review logs."
            if partial
            else result
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
        if self._retention_task is None or self._retention_task.done():
            self._retention_task = asyncio.create_task(self._retention_loop(stores))

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
