"""User entry points and durable execution for deep web research."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from chatcore.access import AccessPolicy
from chatcore.command_prompts import CommandReply
from chatcore.display_time import format_instant
from chatcore.display_time_store import display_service
from chatcore.events import ChatRef, IncomingMessage, MessageRef, User
from chatcore.limits import Limits
from chatcore.llm.base import Provider, ProviderError
from chatcore.llm.types import Usage
from chatcore.platform import ChatPlatform
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.skills import Skill
from chatcore.tools.registry import Tool, ToolContext, ToolResult, TurnState
from chatcore_web import WebPlugin, WebSettings

from chatcore_research.agent import ResearchAgent, ResearchBudget, ResearchProgress, ResearchTimeout
from chatcore_research.jobs import ResearchJob, ResearchJobs
from chatcore_research.quota import AllowAllResearch, ResearchAccess, ResearchQuota
from chatcore_research.settings import ResearchSettings
from chatcore_research.worker import ResearchWorker

log = logging.getLogger(__name__)
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_MENTION_COMMAND = re.compile(r"^/research(?:@\w+)?(?:\s+|$)", re.IGNORECASE)
SEND_DEADLINE_S = 30
Sender = Callable[[str, str, str | None], Awaitable[MessageRef]]


# Default behaviour text for the `research` skill; apps may add to or override it.
RESEARCH_INSTRUCTIONS = (
    "For questions that need several searches or comparing sources, call research once "
    "instead of making many search_web or fetch_web_page calls."
)


class ResearchPlugin(BasePlugin):
    name = "research"
    help_title = "Research"
    migrations_package = "chatcore_research.migrations"

    def skills(self):
        return [
            Skill(
                "research",
                "Research a question across sources",
                (
                    "research this topic",
                    "compare the evidence",
                    "研究一下这个问题",
                    "找资料并总结",
                    "check properly can",
                ),
                ("research",),
                keywords=("research", "sources", "研究", "资料"),
                max_steps=2,
                instructions=RESEARCH_INSTRUCTIONS,
            )
        ]

    def __init__(
        self,
        settings: ResearchSettings,
        *,
        web_settings: WebSettings,
        provider: Provider,
        limits: Limits,
        access: AccessPolicy,
        platform: ChatPlatform,
        sender: Sender,
        jobs: ResearchJobs | None = None,
        research_access: ResearchAccess | None = None,
        usage_recorder=None,
        bot_name: str,
        clock=lambda: datetime.now(UTC),
        sleep=asyncio.sleep,
        monotonic=None,
        typing_interval_s: float = 4,
        edit_interval_s: float = 2,
        lease_check_interval_s: float = 5,
    ) -> None:
        self.settings, self.web_settings, self.provider = settings, web_settings, provider
        self.limits, self.access, self.platform, self.sender = limits, access, platform, sender
        self.jobs = jobs
        self.research_access = research_access or AllowAllResearch()
        self.usage_recorder, self.bot_name, self.clock = usage_recorder, bot_name, clock
        self.sleep = sleep
        self.monotonic = monotonic
        self.typing_interval_s, self.edit_interval_s = typing_interval_s, edit_interval_s
        self.lease_check_interval_s = lease_check_interval_s
        self.quota = ResearchQuota(limits.cache, limits, settings)
        self.worker: ResearchWorker | None = None
        self.stores = None
        self._tasks: set[asyncio.Task] = set()
        self._inline_chats: dict[str, str] = {}
        self._purged_inline: set[tuple[str, str]] = set()
        self._closing = False

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "research",
                "Research multi-source or comparison questions in one call. "
                "Not for one fact → search_web.",
                {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "description": "Research question or topic.",
                            "type": "string",
                            "maxLength": self.settings.topic_max_chars,
                        }
                    },
                    "required": ["topic"],
                    "additionalProperties": False,
                },
                self._tool_research,
            )
        ]

    def commands(self) -> list[Command]:
        return [
            Command(
                "research",
                "Deep web research: /research <topic>, --long, status, cancel",
                self._command,
            )
        ]

    def wrap_responder(self, inner):
        plugin = self

        class ResearchResponder:
            async def respond(self, message: IncomingMessage, text: str, app) -> None:
                if not _MENTION_COMMAND.match(text):
                    if inner:
                        await inner.respond(message, text, app)
                    return
                args = text[_MENTION_COMMAND.match(text).end() :].strip()
                task = asyncio.create_task(plugin._handle_text(message, args, app.stores))
                plugin._tasks.add(task)
                task.add_done_callback(plugin._finish_responder_task)

            async def consider(self, message: IncomingMessage, app) -> None:
                if inner:
                    await inner.consider(message, app)

        return ResearchResponder()

    def _finish_responder_task(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            log.warning("research responder failed error=%s", type(error).__name__)

    async def _command(self, ctx: CommandContext, args: str) -> str | CommandReply | None:
        if ctx.message is None:
            return "Research needs a message context."
        if not args.strip():
            return CommandReply(
                "What should I research? Reply to this message with a topic. Start it with "
                "--long for a longer report.",
                awaits_reply=True,
                placeholder="Research topic",
            )
        await self._handle_text(ctx.message, args, ctx.stores)
        return None

    async def _handle_text(self, message: IncomingMessage, args: str, stores) -> None:
        value = args.strip()
        if not value:
            await self._reply(
                message,
                stores,
                "Usage: /research <topic>, /research --long <topic>, "
                "/research status, or /research cancel",
            )
            return
        if value.casefold() == "status":
            await self._reply(message, stores, await self._status(message, stores))
            return
        if value.casefold() == "cancel":
            job = (
                await self.jobs.cancel(message.chat.key, message.user.ref.key)
                if self.jobs
                else None
            )
            text = (
                f"Cancelled research {str(job.id).replace('-', '')[:8]}."
                if job
                else "You have no active research in this chat."
            )
            await self._reply(message, stores, text)
            return
        background = value.startswith("--long ")
        topic_raw = value[7:].strip() if background else value
        topic = self._topic(topic_raw)
        if topic is None:
            await self._reply(
                message,
                stores,
                f"Please provide a topic of at most {self.settings.topic_max_chars} characters.",
            )
            return
        seed_urls = await self._human_urls(message, stores)
        if background:
            await self._start_background(message, stores, topic, seed_urls, count_quota=True)
            return
        context = ToolContext(
            message.chat,
            message.user,
            message.is_dm,
            self.platform,
            stores,
            message.thread_id,
            TurnState(seen_urls=set(seed_urls)),
            self.access.is_admin(message.user.ref),
        )
        await self._run_inline(
            message,
            context,
            topic,
            seed_urls,
            self.settings.inline_timeout_s,
            ResearchBudget(
                self.settings.inline_max_searches,
                self.settings.inline_max_fetches,
                self.settings.inline_timeout_s,
                self.settings.inline_token_ceiling,
            ),
            post_report=True,
            count_quota=True,
        )

    async def _tool_research(self, ctx: ToolContext, topic: str) -> ToolResult:
        cleaned = self._topic(topic)
        if cleaned is None:
            return ToolResult.error(
                f"Please provide a topic of at most {self.settings.topic_max_chars} characters."
            )
        seed_urls = tuple(ctx.turn.seen_urls)
        result = await self._run_inline(
            None,
            ctx,
            cleaned,
            seed_urls,
            self.settings.tool_timeout_s,
            ResearchBudget(
                self.settings.inline_max_searches,
                self.settings.inline_max_fetches,
                self.settings.tool_timeout_s,
                self.settings.inline_token_ceiling,
            ),
            post_report=False,
            count_quota=True,
        )
        return result

    async def _run_inline(
        self, message, ctx, topic, seed_urls, timeout_s, budget, *, post_report, count_quota
    ):
        token = await self.quota.acquire_inline(ctx.user.ref)
        if token is None:
            return await self._inline_refusal(
                message,
                ctx,
                await self._active_refusal(ctx.chat, ctx.user.ref),
                post_report,
            )
        task = asyncio.current_task()
        status_ref = None
        typing_task = None
        last_edit = 0.0
        last_text = ""
        background_created = False
        checks_passed = False

        async def edit_status(text: str) -> None:
            nonlocal last_text
            if status_ref is not None and self.platform.capabilities.edit and text != last_text:
                # Status edits are best effort: a deleted status message must not lose a report.
                try:
                    await self.platform.edit(status_ref, text)
                except Exception as exc:
                    log.warning("research status edit failed error=%s", type(exc).__name__)
                last_text = text

        async def progress(value: ResearchProgress) -> None:
            nonlocal last_edit, last_text
            text = (
                "Researching… (writing report)"
                if value.phase == "writing"
                else (
                    f"Researching… (searching {value.searches}/{value.max_searches}, "
                    f"reading {value.fetches}/{value.max_fetches})"
                )
            )
            now = self._monotonic()
            if (
                status_ref is not None
                and self.platform.capabilities.edit
                and text != last_text
                and now - last_edit >= self.edit_interval_s
            ):
                try:
                    await self.platform.edit(status_ref, text)
                except Exception as exc:
                    log.warning("research status edit failed error=%s", type(exc).__name__)
                last_text, last_edit = text, now

        try:
            if task is not None:
                self._tasks.add(task)
            self._inline_chats[ctx.user.ref.key] = ctx.chat.key
            refusal = await self._start_check(
                ctx.chat, ctx.user.ref, count_quota=count_quota, ignore_inline_slot=True
            )
            if refusal:
                return await self._inline_refusal(message, ctx, refusal, post_report)
            checks_passed = True
            status_text = (
                f"Researching… (searching 0/{budget.max_searches})"
                if post_report
                else "Researching…"
            )
            status_ref = await self._send(
                ctx.chat, ctx.stores, status_text, thread_id=ctx.thread_id
            )
            typing_task = asyncio.create_task(self._typing_loop(ctx.chat, ctx.thread_id))
            agent = self._agent(ctx, budget, on_usage=self._usage_callback(ctx.chat, ctx.user.ref))
            report = await agent.run(
                topic,
                ctx=ctx,
                budget=budget,
                seed_urls=seed_urls,
                on_progress=progress,
                session_key=self._session_key(ctx.chat, ctx.thread_id),
                budget_ok=self._budget_ok,
            )
            rendered = report.render()
            if not await self.research_access.can_research(ctx.chat, ctx.user.ref):
                denied = "Research isn't available to you right now."
                if post_report:
                    await self._reply(message, ctx.stores, denied)
                    return None
                return ToolResult.error(denied)
            if post_report:
                await self._send(
                    ctx.chat,
                    ctx.stores,
                    rendered,
                    reply_to=message.ref if message else None,
                    thread_id=ctx.thread_id,
                )
                await edit_status("Research done.")
                return None
            await edit_status("Research done.")
            # Only the research agent's verified source catalog grants public URL provenance.
            # It deliberately never populates human_urls used for authenticated file access.
            ctx.turn.seen_urls.update(source.url for source in report.sources)
            return ToolResult(rendered, untrusted=True)
        except ResearchTimeout:
            if post_report and self.jobs:
                job = await self._handoff(ctx, topic, seed_urls)
                background_created = job is not None
                if job:
                    await edit_status("Research queued for background completion.")
                    await self._reply(
                        message,
                        ctx.stores,
                        "This needs longer research — I'll post the report here when it's done.",
                    )
                else:
                    await edit_status("Research timed out.")
                    await self._reply(
                        message, ctx.stores, "Research ran out of time; try a narrower topic."
                    )
            elif not post_report and self.jobs:
                job = await self._handoff(ctx, topic, seed_urls)
                background_created = job is not None
                await edit_status(
                    "Research queued for background completion." if job else "Research timed out."
                )
                return (
                    ToolResult(
                        "Inline research reached its time limit. A background report will be "
                        "posted to this chat later; tell the user."
                    )
                    if job
                    else ToolResult.error("Research ran out of time; try a narrower topic.")
                )
            else:
                if post_report:
                    await self._reply(
                        message, ctx.stores, "Research ran out of time; try a narrower topic."
                    )
                else:
                    return ToolResult.error("Research ran out of time; try a narrower topic.")
        except asyncio.CancelledError:
            queued = False
            if (
                checks_passed
                and not post_report
                and self.jobs
                and not self._closing
                and not background_created
            ):
                try:
                    queued_job = await asyncio.shield(self._handoff(ctx, topic, seed_urls))
                    queued = queued_job is not None
                except Exception as exc:
                    log.warning("research cancellation enqueue failed error=%s", type(exc).__name__)
            try:
                await edit_status(
                    "Research continues in the background." if queued else "Research stopped."
                )
            except Exception as exc:
                log.warning("research cancellation status failed error=%s", type(exc).__name__)
            raise
        except ProviderError:
            if post_report:
                await self._reply(
                    message,
                    ctx.stores,
                    "Research couldn't reach the language model. Please try again later.",
                )
            else:
                return ToolResult.error(
                    "Research couldn't reach the language model. Please try again later."
                )
        except Exception as exc:
            if status_ref is None:
                raise
            log.warning("research inline failed error=%s", type(exc).__name__)
            if post_report:
                await self._reply(
                    message, ctx.stores, "Research couldn't be completed. Please try again later."
                )
            else:
                return ToolResult.error("Research couldn't be completed. Please try again later.")
        finally:
            if typing_task is not None:
                typing_task.cancel()
                await asyncio.gather(typing_task, return_exceptions=True)
            await self.quota.release_inline(ctx.user.ref, token)
            self._inline_chats.pop(ctx.user.ref.key, None)
            self._purged_inline.discard((ctx.chat.key, ctx.user.ref.key))
            if task is not None:
                self._tasks.discard(task)
        return None

    async def _handoff(self, ctx, topic, seed_urls):
        # /forgetme or /purgechat during an inline run must not re-create the deleted topic.
        if (ctx.chat.key, ctx.user.ref.key) in self._purged_inline:
            return None
        return await self.jobs.create(
            ctx.chat, ctx.user.ref, ctx.thread_id, topic, seed_urls, is_dm=ctx.is_dm
        )

    async def _inline_refusal(self, message, ctx, text, post_report):
        if post_report:
            await self._reply(message, ctx.stores, text)
            return None
        return ToolResult.error(text)

    async def _start_check(self, chat, user, *, count_quota, ignore_inline_slot=False):
        if not await self.research_access.can_research(chat, user):
            return "Research isn't available to you right now."
        active = await self.jobs.active_for(user.key) if self.jobs else None
        if active:
            return (
                "You already have research running in another chat."
                if active.chat != chat
                else "You already have research running."
            )
        inline = await self.limits.cache.get(f"research:active:{user.key}")
        if inline is not None and not ignore_inline_slot:
            other_chat = self._inline_chats.get(user.key)
            return (
                "You already have research running in another chat."
                if other_chat != chat.key
                else "You already have research running."
            )
        if count_quota:
            return await self.quota.check_and_count(chat, user)
        return None

    async def _start_background(self, message, stores, topic, seed_urls, *, count_quota):
        if not self.jobs:
            await self._reply(message, stores, "Background research isn't available here.")
            return
        token = await self.quota.acquire_inline(message.user.ref)
        if token is None:
            await self._reply(
                message, stores, await self._active_refusal(message.chat, message.user.ref)
            )
            return
        self._inline_chats[message.user.ref.key] = message.chat.key
        try:
            refusal = await self._start_check(
                message.chat,
                message.user.ref,
                count_quota=count_quota,
                ignore_inline_slot=True,
            )
            if refusal:
                await self._reply(message, stores, refusal)
                return
            job = await self.jobs.create(
                message.chat,
                message.user.ref,
                message.thread_id,
                topic,
                seed_urls,
                is_dm=message.is_dm,
            )
            if job is None:
                active = await self.jobs.active_for(message.user.ref.key)
                text = (
                    "You already have research running in another chat."
                    if active and active.chat != message.chat
                    else "You already have research running."
                )
            else:
                suffix = (
                    f" Use /research@{self.bot_name} status or /research@{self.bot_name} cancel."
                    if not message.is_dm
                    else " Use /research status or /research cancel."
                )
                text = "Researching — I'll post the report here when done." + suffix
            await self._reply(message, stores, text)
        finally:
            await self.quota.release_inline(message.user.ref, token)
            self._inline_chats.pop(message.user.ref.key, None)

    async def _status(self, message, stores):
        if not self.jobs:
            return "You have no research jobs in this chat."
        jobs = await self.jobs.list_for(message.chat.key, message.user.ref.key, 5)
        if not jobs:
            text = "You have no research jobs in this chat."
        else:
            zone, _ = await display_service(stores).resolve(
                message.chat, message.user.ref, is_dm=message.is_dm
            )
            rows = []
            for job in jobs:
                safe_topic = re.sub(r"\s+", " ", _URL.sub("[link removed]", job.topic)).strip()
                rows.append(
                    f"{str(job.id).replace('-', '')[:8]} · {job.status} · "
                    f"{format_instant(job.created_at, zone)} · "
                    f"{safe_topic[:60]}"
                )
            text = "\n".join(rows)
        active = await self.jobs.active_for(message.user.ref.key)
        if active and active.chat != message.chat:
            text += "\nYou also have research running in another chat."
        return text

    async def _active_refusal(self, chat, user):
        active = await self.jobs.active_for(user.key) if self.jobs else None
        if active and active.chat != chat:
            return "You already have research running in another chat."
        inline_chat = self._inline_chats.get(user.key)
        if inline_chat is not None and inline_chat != chat.key:
            return "You already have research running in another chat."
        return "You already have research running."

    def _agent(self, ctx, budget, *, on_usage):
        web = WebPlugin(
            self.web_settings.model_copy(update={"max_fetches_per_turn": budget.max_fetches})
        )
        return ResearchAgent(self.provider, web, self.settings, on_usage=on_usage)

    def _usage_callback(self, chat, user):
        async def record(usage: Usage):
            await self.limits.record(usage.input_tokens + usage.output_tokens)
            if self.usage_recorder:
                await self.usage_recorder(
                    chat,
                    user,
                    getattr(self.provider, "name", type(self.provider).__name__),
                    getattr(self.provider, "model", ""),
                    usage.input_tokens,
                    usage.output_tokens,
                )

        return record

    async def _human_urls(self, message, stores):
        urls = [item.rstrip(".,;:!?)]}") for item in _URL.findall(message.text)]
        if message.reply_to:
            row = await stores.repo.get_message(message.chat, message.reply_to.message_id)
            if row and not row.is_bot:
                urls.extend(item.rstrip(".,;:!?)]}") for item in _URL.findall(row.text))
        return tuple(urls)

    def _topic(self, raw):
        if not isinstance(raw, str):
            return None
        topic = re.sub(r"\s+", " ", raw).strip()
        return topic if topic and len(topic) <= self.settings.topic_max_chars else None

    @staticmethod
    def _session_key(chat: ChatRef, thread_id: str | None) -> str:
        thread = f":{thread_id}" if thread_id else ""
        return f"{chat.key}{thread}:research"

    async def _send(self, chat, stores, text, *, reply_to=None, thread_id=None):
        ref = await self.platform.send(chat, text, reply_to=reply_to, thread_id=thread_id)
        await stores.repo.log_bot_message(chat, ref.message_id, text, thread_id=thread_id)
        return ref

    async def _reply(self, message, stores, text):
        return await self._send(
            message.chat, stores, text, reply_to=message.ref, thread_id=message.thread_id
        )

    async def _typing_loop(self, chat, thread_id):
        while True:
            await self.platform.typing(chat, thread_id)
            await self.sleep(self.typing_interval_s)

    def _monotonic(self):
        return self.monotonic() if self.monotonic else asyncio.get_running_loop().time()

    async def _budget_ok(self):
        return not await self.limits.budget_exhausted()

    async def on_startup(self, stores):
        self.stores = stores
        if self.jobs:
            self.worker = ResearchWorker(
                self.jobs,
                self._run_job,
                settings=self.settings,
                clock=self.clock,
                sleep=self.sleep,
                can_claim=self._budget_ok,
            )
            self.worker.start()

    async def _authorized(self, job):
        try:
            await self.access.refresh()
        except Exception as exc:
            log.warning("research access refresh failed error=%s", type(exc).__name__)
            return False
        if job.is_dm:
            allowed = await self.access.can_dm(job.owner, self.platform, self.limits.cache)
        else:
            allowed = self.access.is_chat_allowed(job.chat)
        return allowed and await self.research_access.can_research(job.chat, job.owner)

    async def _run_job(self, job: ResearchJob):
        if not await self._authorized(job):
            await self.jobs.finish(job, status="cancelled", delivery_state="suppressed")
            return
        if job.status == "failed" and job.error_class == "AttemptsExhausted":
            await self._send_job(job, "Research couldn't be completed (an internal error).")
            return
        if not await self.jobs.still_owned(job):
            return
        if await self.limits.budget_exhausted():
            await self.jobs.release(job)
            return
        remaining = self.settings.long_token_ceiling - job.tokens_used
        if remaining <= 0:
            finished = await self.jobs.finish(
                job, status="failed", delivery_state=None, error_class="TokenCeiling"
            )
            if finished and await self._authorized(job):
                await self._send_job(job, "Research couldn't be completed (token limit).")
            return
        context = ToolContext(
            job.chat,
            User(job.owner, job.owner.id),
            job.is_dm,
            self.platform,
            self.stores,
            job.thread_id,
            TurnState(seen_urls=set(job.seed_urls)),
        )
        budget = ResearchBudget(
            self.settings.long_max_searches,
            self.settings.long_max_fetches,
            self.settings.long_timeout_s,
            remaining,
        )

        async def usage(delta):
            await self.limits.record(delta.input_tokens + delta.output_tokens)
            await self.jobs.add_tokens(job, delta.input_tokens + delta.output_tokens)
            if self.usage_recorder:
                await self.usage_recorder(
                    job.chat,
                    job.owner,
                    getattr(self.provider, "name", type(self.provider).__name__),
                    getattr(self.provider, "model", ""),
                    delta.input_tokens,
                    delta.output_tokens,
                )

        research_task: asyncio.Task | None = None

        async def watch_lease():
            while True:
                await self.sleep(self.lease_check_interval_s)
                if not await self.jobs.still_owned(job):
                    if research_task is not None:
                        research_task.cancel()
                    return

        watcher = asyncio.create_task(watch_lease())
        try:
            research_task = asyncio.create_task(
                self._agent(context, budget, on_usage=usage).run(
                    job.topic,
                    ctx=context,
                    budget=budget,
                    seed_urls=job.seed_urls,
                    should_continue=lambda: self.jobs.still_owned(job),
                    session_key=self._session_key(job.chat, job.thread_id),
                    budget_ok=self._budget_ok,
                )
            )
            report = await research_task
            if not await self._authorized(job):
                await self.jobs.finish(job, status="cancelled", delivery_state="suppressed")
                return
            if not await self.jobs.reserve_delivery(job):
                return
            try:
                ref = await self._send_job(job, report.render())
            except asyncio.CancelledError:
                # Interrupted mid-send: the outcome is unknown and is never replayed.
                await asyncio.shield(
                    self.jobs.finish(job, status="completed", delivery_state="unknown")
                )
                raise
            except Exception:
                await self.jobs.finish(job, status="completed", delivery_state="unknown")
                return
            await self.jobs.finish(
                job,
                status="completed",
                delivery_state="sent",
                message_id=getattr(ref, "message_id", None),
            )
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current and current.cancelling():
                raise
            return
        except Exception as exc:
            finished = await self.jobs.finish(
                job, status="failed", delivery_state=None, error_class=type(exc).__name__
            )
            if finished and await self._authorized(job):
                await self._send_job(job, self._failure_notice(exc))
            log.warning("research job failed id=%s error=%s", job.id, type(exc).__name__)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if research_task is not None and not research_task.done():
                research_task.cancel()
                await asyncio.gather(research_task, return_exceptions=True)

    async def _send_job(self, job, text):
        # A stalled platform send must not hold the worker slot forever.
        async with asyncio.timeout(SEND_DEADLINE_S):
            return await self.sender(job.chat.key, text, job.thread_id)

    @staticmethod
    def _failure_notice(exc):
        if isinstance(exc, ResearchTimeout):
            reason = "time limit"
        elif isinstance(exc, ProviderError):
            reason = "the language model was unavailable"
        else:
            reason = "an internal error"
        return f"Research couldn't be completed ({reason})."

    async def purge_user(self, chat, user, stores):
        if self._inline_chats.get(user.key) == chat.key:
            self._purged_inline.add((chat.key, user.key))
        if self.jobs:
            await self.jobs.purge_user(chat.key, user.key)

    async def purge_chat(self, chat, stores):
        for user_key, chat_key in self._inline_chats.items():
            if chat_key == chat.key:
                self._purged_inline.add((chat_key, user_key))
        if self.jobs:
            await self.jobs.purge_chat(chat.key)

    async def purge_older_than(self, cutoff, stores):
        if self.jobs:
            research_cutoff = self.clock() - timedelta(days=self.settings.retention_days)
            await self.jobs.purge_older_than(max(cutoff, research_cutoff))

    async def aclose(self):
        self._closing = True
        if self.worker:
            await self.worker.aclose()
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
