"""Shared validation and authorization for commands, confirmed tools and closing."""

import logging
from datetime import UTC, datetime

from chatcore.display_time import format_instant
from chatcore.display_time_store import display_service
from chatcore.events import MessageRef
from chatcore.pending import PendingAction
from chatcore.platform import PlatformError
from chatcore.plugin import CommandContext
from chatcore.polls import PollLimits, PollPlatform
from chatcore.skills import render_hint
from chatcore.store.base import Stores
from chatcore.store.pg import PgDatabase
from chatcore.tools.registry import ToolContext

from chatcore_polls.models import Poll, PollDraft, Scope
from chatcore_polls.permissions import is_group_manager
from chatcore_polls.repository import PollsRepo
from chatcore_polls.results import format_results
from chatcore_polls.settings import PollsSettings
from chatcore_polls.timing import parse_close_time

log = logging.getLogger(__name__)

POLLS_OFF_HINT = (
    "Polls are off in this group. A group setup manager or admin can turn them on: they can ask "
    "me to turn on polls, or send {cmd:polltools} on."
)
"""Shared by the skill gate (routing hint) and the call-time refusal, so both say the same."""


def build_draft(
    question: str,
    options: list[str] | tuple[str, ...],
    *,
    limits: PollLimits,
    settings: PollsSettings,
    now: datetime,
    multiple: bool = False,
    anonymous: bool = False,
    close_local_time: str | None = None,
    close_timezone: str | None = None,
) -> PollDraft:
    if type(multiple) is not bool or type(anonymous) is not bool:
        raise ValueError("Poll choice and anonymity flags must be boolean.")
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= limits.max_question_len:
        raise ValueError(f"Question must be 1–{limits.max_question_len} characters.")
    maximum = min(settings.max_options, limits.max_options)
    if not isinstance(options, (list, tuple)) or not 2 <= len(options) <= maximum:
        raise ValueError(f"Provide 2–{maximum} options.")
    if any(
        not isinstance(o, str) or not 1 <= len(o.strip()) <= limits.max_option_len for o in options
    ):
        raise ValueError(f"Each option must be 1–{limits.max_option_len} characters.")
    stripped = tuple(o.strip() for o in options)
    if len({o.casefold() for o in stripped}) != len(stripped):
        raise ValueError("Options must be distinct (ignoring case).")
    if (close_local_time is None) != (close_timezone is None):
        raise ValueError("Provide both a close local time and an IANA timezone.")
    parsed = None
    if close_local_time is not None:
        parsed = parse_close_time(
            close_local_time,
            close_timezone,
            now,
            horizon_days=settings.close_horizon_days,
            min_lead_seconds=settings.min_close_lead_seconds,
        )
    return PollDraft(
        question.strip(),
        stripped,
        multiple,
        anonymous,
        parsed.instant if parsed else None,
        parsed.label if parsed else None,
    )


class PollsService:
    def __init__(self, settings, access, platform, clock=lambda: datetime.now(UTC)):
        self.settings, self.access, self.platform, self.clock = settings, access, platform, clock

    def repo(self, stores: Stores) -> PollsRepo:
        if not isinstance(stores.db, PgDatabase):
            raise RuntimeError("Polls require PostgreSQL.")
        return PollsRepo(stores.db, self.settings)

    def scope(self, ctx: CommandContext | ToolContext) -> Scope:
        if ctx.is_dm:
            raise PermissionError("Polls work in group chats.")
        if not isinstance(self.platform, PollPlatform):
            raise PermissionError("Polls are not supported on this platform.")
        if (
            ctx.chat.platform != self.platform.name
            or ctx.user.ref.platform != ctx.chat.platform
            or not self.access.is_chat_allowed(ctx.chat)
        ):
            raise PermissionError("Polls are not available in this group.")
        return Scope(ctx.chat.key, ctx.is_dm, ctx.thread_id)

    async def ready(self, ctx, *, enabled: bool = False):
        scope = self.scope(ctx)
        current = await self.repo(ctx.stores).settings(scope)
        if enabled and not current["polls_enabled"]:
            raise PermissionError(render_hint(POLLS_OFF_HINT, ctx.command_form))
        return scope, current

    def build_draft(self, **values) -> PollDraft:
        return build_draft(
            **values, limits=self.platform.poll_limits, settings=self.settings, now=self.clock()
        )

    def _action(self, ctx, scope, current, kind, data, summary):
        return PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin="polls",
            kind=kind,
            payload={
                "scope": {
                    "chat_key": scope.chat_key,
                    "is_dm": scope.is_dm,
                    "thread_id": scope.thread_id,
                },
                "requester": ctx.user.ref.key,
                "revision": current["revision"],
                **data,
            },
            summary=summary,
            thread_id=ctx.thread_id,
            now=self.clock(),
        )

    async def request_create(self, ctx, values) -> PendingAction:
        scope, current = await self.ready(ctx, enabled=True)
        draft = self.build_draft(**values)
        destination = scope.chat_key + (f" topic {scope.thread_id}" if scope.thread_id else "")
        summary = (
            f"Post {'an anonymous' if draft.anonymous else 'a named'}, "
            f"{'multiple-choice' if draft.multiple else 'single-choice'} poll in {destination}:\n"
            f"Question: {draft.question}\n"
            + "\n".join(f"{i}. {option}" for i, option in enumerate(draft.options, 1))
            + f"\nCloses: {draft.close_label or 'when closed manually'}"
        )
        data = {
            "question": draft.question,
            "options": list(draft.options),
            "multiple": draft.multiple,
            "anonymous": draft.anonymous,
            "close_at": draft.close_at.isoformat() if draft.close_at else None,
            "close_local_time": values.get("close_local_time"),
            "close_timezone": values.get("close_timezone"),
        }
        return self._action(ctx, scope, current, "create", {"draft": data}, summary)

    async def request_configure(self, ctx, enabled: bool) -> PendingAction:
        scope = self.scope(ctx)
        if not await is_group_manager(self.access, self.platform, scope, ctx.user.ref):
            raise PermissionError("Only a current group manager can change poll settings.")
        # The manager lookup refreshes access; re-check the allowlist before proposing.
        self.scope(ctx)
        if type(enabled) is not bool:
            raise ValueError("Poll enablement must be boolean.")
        current = await self.repo(ctx.stores).settings(scope)
        return self._action(
            ctx,
            scope,
            current,
            "configure",
            {"enabled": enabled},
            f"Turn polls {'on' if enabled else 'off'} in {scope.chat_key}. "
            "Open polls stay open; turning polls off blocks new polls.",
        )

    async def send(
        self, ctx, scope, draft, *, source_action_id=None, revision=None, commit_echo=False
    ):
        repo = self.repo(ctx.stores)
        poll, created = await repo.reserve(
            scope,
            ctx.user,
            draft,
            source_action_id,
            self.clock(),
            expected_revision=revision,
        )
        if not created:
            return (
                f"Poll #{poll.number} was already posted."
                if poll.status == "open"
                else "That poll request was already handled; nothing else was posted."
            )
        try:
            sent = await self.platform.send_poll(
                scope.chat,
                draft.question,
                draft.options,
                multiple=draft.multiple,
                anonymous=draft.anonymous,
                thread_id=scope.thread_id,
            )
        except PlatformError as exc:
            await repo.mark_failed(scope, poll.id)
            log.warning("poll send failed id=%s error=%s", poll.id, type(exc).__name__)
            return "Couldn't send the poll; nothing was posted."
        await repo.mark_open(scope, poll.id, sent.message.message_id, sent.poll_id)
        await self.log_message(
            ctx.stores,
            sent.message,
            f"Poll #{poll.number}: {draft.question} — options: " + " | ".join(draft.options),
        )
        if commit_echo:
            close = f"; it closes at {draft.close_label}" if draft.close_at else ""
            return (
                f"Poll #{poll.number} posted{close}. To close it early, reply to it with "
                f"/closepoll or use /closepoll {poll.number}."
            )
        if draft.close_at:
            return f"Poll #{poll.number} closes at {draft.close_label}."
        return None

    @staticmethod
    async def log_message(stores, message, text):
        try:
            await stores.repo.log_bot_message(
                message.chat,
                message.message_id,
                text,
                thread_id=message.thread_id,
            )
        except Exception as exc:
            log.warning("poll history logging failed: %s", type(exc).__name__)

    async def list_open(self, ctx) -> str:
        scope, _ = await self.ready(ctx)
        polls = await self.repo(ctx.stores).list_open(scope)
        if not polls:
            return "No open polls here."
        zone, _ = await display_service(ctx.stores).resolve(ctx.chat, ctx.user.ref, is_dm=ctx.is_dm)
        return "\n".join(
            f"#{p.number} · {p.question} · {'multi' if p.multiple else 'single'} · "
            f"{'anon' if p.anonymous else 'named'} · closes "
            + (format_instant(p.close_at, zone) if p.close_at else "manually")
            for p in polls
        )

    async def _may_close(self, ctx, scope: Scope, poll: Poll) -> bool:
        return poll.creator_key == ctx.user.ref.key or await is_group_manager(
            self.access, self.platform, scope, ctx.user.ref
        )

    async def close(self, ctx, number: int | None) -> str | None:
        scope, _ = await self.ready(ctx)
        repo = self.repo(ctx.stores)
        if number is not None:
            poll = await repo.get_by_number(scope, number)
        else:
            reply = ctx.message.reply_to if ctx.message else None
            if reply is None or reply.chat != ctx.chat or reply.thread_id != ctx.thread_id:
                return "Use /closepoll <number>, or reply to the poll with /closepoll."
            poll = await repo.get_by_message(scope, reply.message_id)
        if poll is None or poll.status != "open":
            return "No open poll found here."
        if not await self._may_close(ctx, scope, poll):
            return "Only the poll creator or a group manager can close this poll."
        self.scope(ctx)
        claims = await repo.claim_close(poll.id, self.clock(), 1, scope=scope)
        if not claims:
            return "That poll is already closing."
        return await self.close_claim(claims[0], ctx.stores)

    async def request_close(self, ctx, number: int) -> PendingAction:
        """Propose closing an open poll in this topic; the commit re-checks everything."""
        if type(number) is not int or number < 1:
            raise ValueError("Give the poll number from list_polls.")
        scope, current = await self.ready(ctx)
        poll = await self.repo(ctx.stores).get_by_number(scope, number)
        if poll is None or poll.status != "open":
            raise ValueError("No open poll with that number here.")
        if not await self._may_close(ctx, scope, poll):
            raise PermissionError("Only the poll creator or a group manager can close this poll.")
        self.scope(ctx)
        return self._action(
            ctx,
            scope,
            current,
            "close",
            {"poll_id": str(poll.id), "number": poll.number},
            f"Close poll #{poll.number} ({poll.question}) and post the results.",
        )

    async def close_claim(self, poll: Poll, stores: Stores) -> str | None:
        if not self.access.is_chat_allowed(poll.scope.chat) or poll.scope.is_dm:
            return None
        repo = self.repo(stores)
        counts, total = None, None
        try:
            result = await self.platform.stop_poll(
                MessageRef(
                    poll.scope.chat,
                    poll.platform_message_id,
                    poll.scope.thread_id,
                )
            )
            counts, total = result.counts, result.total_voters
        except PlatformError as exc:
            log.warning("poll stop failed id=%s error=%s", poll.id, type(exc).__name__)
            failures = await repo.record_close_failure(poll.scope, poll.id, poll.lease_token)
            if failures is None:
                return None
            if failures < self.settings.max_close_failures:
                return "Couldn't close the poll yet; it will be retried."
        if not await repo.finish_close(
            poll.scope,
            poll.id,
            poll.lease_token,
            counts,
            total,
            self.clock(),
        ):
            return None
        votes = await repo.votes(poll.scope, poll.id) if not poll.anonymous else []
        text = format_results(
            poll, counts, votes, max_length=self.platform.capabilities.max_message_len
        )
        try:
            ref = await self.platform.send(poll.scope.chat, text, thread_id=poll.scope.thread_id)
        except PlatformError as exc:
            log.warning("poll results send failed id=%s error=%s", poll.id, type(exc).__name__)
            return None
        await self.log_message(stores, ref, text)
        return None

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        rejected = "Nothing was posted. Request the poll or setting change again."
        try:
            scope = self.scope(ctx)
            payload = action.payload
            if (
                action.plugin != "polls"
                or action.chat != ctx.chat
                or action.requester != ctx.user.ref
                or action.thread_id != ctx.thread_id
                or payload["scope"]
                != {"chat_key": scope.chat_key, "is_dm": scope.is_dm, "thread_id": scope.thread_id}
                or payload["requester"] != ctx.user.ref.key
                or type(payload["revision"]) is not int
            ):
                return rejected
            if action.kind == "configure":
                if not await is_group_manager(self.access, self.platform, scope, ctx.user.ref):
                    return rejected
                self.scope(ctx)
                result = await self.repo(ctx.stores).configure(
                    scope,
                    payload["enabled"],
                    payload["revision"],
                )
                return f"Polls are {'on' if result['polls_enabled'] else 'off'} in this group."
            if action.kind == "close":
                return await self._commit_close(ctx, scope, payload)
            if action.kind != "create":
                return rejected
            _, current = await self.ready(ctx, enabled=True)
            if current["revision"] != payload["revision"]:
                return rejected
            values = dict(payload["draft"])
            close_at = values.pop("close_at")
            draft = self.build_draft(**values)
            if close_at != (draft.close_at.isoformat() if draft.close_at else None):
                return rejected
            reply = await self.send(
                ctx,
                scope,
                draft,
                source_action_id=action.id,
                revision=payload["revision"],
                commit_echo=True,
            )
            return reply or "Poll posted."
        except (KeyError, TypeError, ValueError, PermissionError):
            return rejected

    async def _commit_close(self, ctx, scope: Scope, payload) -> str:
        number = payload["number"]
        if type(number) is not int:
            raise TypeError("poll number")
        repo = self.repo(ctx.stores)
        poll = await repo.get_by_number(scope, number)
        if poll is None or str(poll.id) != payload["poll_id"] or poll.status != "open":
            return "That poll is no longer open; nothing was changed."
        if not await self._may_close(ctx, scope, poll):
            return "Only the poll creator or a group manager can close this poll."
        self.scope(ctx)
        claims = await repo.claim_close(poll.id, self.clock(), 1, scope=scope)
        if not claims:
            return "That poll is already closing."
        reply = await self.close_claim(claims[0], ctx.stores)
        return reply or f"Poll #{number} closed."


class PollActions:
    def __init__(self, service: PollsService):
        self.service = service

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        return await self.service.commit(action, ctx)
