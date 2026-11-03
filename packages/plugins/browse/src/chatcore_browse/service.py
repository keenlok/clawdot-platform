"""Authorization, confirmation and rendered-fetch orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from unicodedata import category
from urllib.parse import urlsplit, urlunsplit

from chatcore.pending import PendingAction
from chatcore.plugin import CommandContext
from chatcore.security.net_guard import failure_cause
from chatcore.tools.registry import ToolContext, ToolResult
from chatcore_jobs.permissions import can_use_scope, is_group_manager, make_actor, make_scope

from chatcore_browse.client import GENERIC_ERROR, STATUS_MESSAGES, RenderClient
from chatcore_browse.repository import BrowseConflict, BrowseRepo
from chatcore_browse.urls import normalize_url

log = logging.getLogger(__name__)
# TurnState uses slots without weak-reference support, so retain identity briefly and expire it.
_render_counts: dict[int, tuple[object, int, float]] = {}


class BrowseService:
    def __init__(self, settings, access, platform, client=None, clock=None):
        self.settings, self.access, self.platform = settings, access, platform
        self.client = client or RenderClient(settings.render_url or "", settings.request_timeout_s)
        self.clock = clock

    def repo(self, ctx):
        if ctx.stores.db is None:
            raise RuntimeError("Browse settings require PostgreSQL")
        return BrowseRepo(ctx.stores.db, self.settings)

    async def allowed(self, scope, actor) -> bool:
        return await can_use_scope(
            scope,
            actor,
            access=self.access,
            platform=self.platform,
            dm_enabled=self.settings.dm_enabled,
        )

    async def request_configure(self, ctx, enabled: object) -> PendingAction:
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        scope = make_scope(ctx.chat, is_dm=ctx.is_dm, thread_id=ctx.thread_id)
        actor = make_actor(ctx.user.ref)
        if not await is_group_manager(self.access, self.platform, scope, actor):
            raise PermissionError("Only a current group manager can configure rendered fetch.")
        current = await self.repo(ctx).settings(scope)
        desired = "enable" if enabled else "disable"
        summary = (
            f"{desired.capitalize()} rendered page reading for {scope.chat_key}. "
            "Rendered fetch loads pages in a sandboxed headless browser."
        )
        return PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin="browse",
            kind="configure",
            payload={
                "scope": {"chat_key": scope.chat_key, "is_dm": False, "thread_id": scope.thread_id},
                "actor": actor.user_key,
                "enabled": enabled,
                "revision": current["revision"],
            },
            summary=summary,
            thread_id=ctx.thread_id,
        )

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        payload = action.payload
        try:
            scope_data = payload["scope"]
            scope = make_scope(
                ctx.chat,
                is_dm=scope_data["is_dm"],
                thread_id=scope_data.get("thread_id"),
            )
            actor = make_actor(ctx.user.ref)
            if (
                action.plugin != "browse"
                or action.kind != "configure"
                or action.requester != ctx.user.ref
                or scope.chat_key != scope_data["chat_key"]
                or scope.is_dm
                or actor.user_key != payload["actor"]
                or not await is_group_manager(self.access, self.platform, scope, actor)
            ):
                return "Current group manager authorization is required. Nothing changed."
            if type(payload["enabled"]) is not bool or type(payload["revision"]) is not int:
                return "Settings changed, nothing done."
            result = await self.repo(ctx).configure(
                scope,
                enabled=payload["enabled"],
                actor=actor.user_key,
                expected_revision=payload["revision"],
            )
            state = "enabled" if result["rendered_enabled"] else "disabled"
            return f"Rendered page reading {state} for {scope.chat_key}."
        except (BrowseConflict, KeyError, TypeError, ValueError):
            return "Settings changed, nothing done."

    async def fetch(self, ctx: ToolContext, raw_url: object) -> ToolResult:
        scope = make_scope(ctx.chat, is_dm=ctx.is_dm, thread_id=ctx.thread_id)
        actor = make_actor(ctx.user.ref)
        if not await self.allowed(scope, actor):
            return ToolResult.error("Rendered page reading is not allowed here.")
        if ctx.stores.db is None:
            return ToolResult.error(
                "Rendered page reading is disabled here. A group admin can enable it."
            )
        current = await self.repo(ctx).settings(scope)
        if not current["rendered_enabled"]:
            return ToolResult.error(
                "Rendered page reading is disabled here. A group admin can enable it."
            )
        validated = _validate_url(raw_url)
        if validated is None:
            return ToolResult.error("That address is not allowed.")
        original, normalized = validated
        seen = set()
        for item in ctx.turn.seen_urls:
            try:
                seen.add(normalize_url(item))
            except (TypeError, ValueError, UnicodeError):
                continue
        if normalized not in seen:
            return ToolResult.error(
                "I can only open links that someone posted or that came from a search."
            )
        if (
            ctx.turn.fetches >= self.settings.max_fetches_per_turn
            or _render_count(ctx.turn) >= self.settings.max_renders_per_turn
        ):
            return ToolResult.error("This turn has reached its page-fetch limit.")
        ctx.turn.fetches += 1
        _increment_render(ctx.turn)
        parts = urlsplit(original)
        if parts.scheme.lower() == "http":
            host = parts.hostname or ""
            authority = f"[{host}]" if ":" in host else host
            if parts.port is not None and parts.port != 80:
                authority += f":{parts.port}"
            render_url = urlunsplit(("https", authority, parts.path, parts.query, ""))
        else:
            render_url = original
        normalized_render = normalize_url(render_url)
        key = "browse:page:" + hashlib.sha256(normalized_render.encode()).hexdigest()
        cached = await ctx.stores.cache.get(key)
        if cached:
            try:
                page = json.loads(cached)
                if isinstance(page, dict) and isinstance(page.get("content"), str):
                    return ToolResult(
                        content=_truncate_content(page["content"], self.settings.max_chars),
                        untrusted=True,
                    )
            except (ValueError, TypeError):
                pass
        try:
            rendered = await self.client.render(
                render_url, max_response_bytes=self.settings.max_response_bytes
            )
        except Exception as exc:
            log.warning(
                "render failed host=%s error=%s cause=%s",
                urlsplit(render_url).hostname or "",
                type(exc).__name__,
                failure_cause(exc),
            )
            return ToolResult.error(GENERIC_ERROR)
        if rendered.status != "ok":
            message = STATUS_MESSAGES.get(rendered.status, GENERIC_ERROR)
            return ToolResult.error(message)
        prefix = (
            f"Title: {rendered.title}\nURL: {rendered.final_url}\n"
            "Rendered by a headless browser; text only.\n\n"
        )
        content = _truncate_content(
            prefix + rendered.text,
            self.settings.max_chars,
            force=rendered.truncated,
        )
        await ctx.stores.cache.set(
            key, json.dumps({"content": content}).encode(), ttl_s=self.settings.cache_ttl_s
        )
        return ToolResult(content=content, untrusted=True)


def _truncate_content(content: str, max_chars: int, *, force: bool = False) -> str:
    suffix = "\n\n[Truncated]"
    if not force and len(content) <= max_chars:
        return content
    if content.endswith(suffix):
        content = content[: -len(suffix)]
    if max_chars <= len(suffix):
        return suffix[-max_chars:]
    return content[: max_chars - len(suffix)] + suffix


def _validate_url(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if any(category(char) == "Cc" for char in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or port not in {None, 80, 443}
        ):
            return None
        return value, normalize_url(value)
    except (ValueError, UnicodeError):
        return None


def _render_count(turn) -> int:
    entry = _render_counts.get(id(turn))
    if entry is None or entry[0] is not turn or time.monotonic() - entry[2] > 600:
        return 0
    return entry[1]


def _increment_render(turn) -> None:
    now = time.monotonic()
    for key, (_, _, updated) in list(_render_counts.items()):
        if now - updated > 600:
            _render_counts.pop(key, None)
    if id(turn) not in _render_counts and len(_render_counts) >= 4096:
        oldest = min(_render_counts, key=lambda key: _render_counts[key][2])
        _render_counts.pop(oldest, None)
    count = _render_count(turn) + 1
    _render_counts[id(turn)] = (turn, count, now)


class BrowseActions:
    def __init__(self, service: BrowseService) -> None:
        self.service = service

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        return await self.service.commit(action, ctx)
