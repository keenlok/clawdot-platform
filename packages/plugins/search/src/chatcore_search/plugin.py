"""Chat history search and safe attachment reading tools."""

from __future__ import annotations

import asyncio
import io
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

from chatcore.attribution import message_record
from chatcore.command_prompts import CommandReply
from chatcore.embed import Embedder
from chatcore.events import Attachment, ChatRef, IncomingMessage, UserRef
from chatcore.image_reader import ImageReadError, admit_describe
from chatcore.images import ImageUnavailable, download_image, is_image
from chatcore.platform import PlatformError
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.store.base import SearchHit, StoredMessage, Stores
from chatcore.tools.registry import Tool, ToolContext, ToolResult

from chatcore_search.csv_input import parse_csv
from chatcore_search.history import render_history_page
from chatcore_search.settings import SearchSettings

# Pinned to the package name so log records keep their pre-split logger name.
logger = logging.getLogger("chatcore_search")
INDEX = "chunks"
_WINDOW_PREFIX = "window:"
_BOT_PREFIX = "bot:"
_CHUNK_SIZE = 1_600
_CHUNK_OVERLAP = 200
_BOT_ID_CACHE_SIZE = 10_000


MemberGroups = Callable[[UserRef], Awaitable[list[ChatRef]]]


@dataclass(slots=True)
class _WindowBuffer:
    messages: list[IncomingMessage] = field(default_factory=list)
    partial_indexed: bool = False
    dirty: bool = False


class SearchPlugin(BasePlugin):
    name = "search"
    help_title = "Search"
    Settings = SearchSettings

    def __init__(
        self,
        embedder: Embedder,
        *,
        member_groups: MemberGroups | None = None,
        settings: SearchSettings | None = None,
        flush_interval_s: float = 30,
    ) -> None:
        self.embedder = embedder
        self.member_groups = member_groups
        self.settings = settings or SearchSettings()
        self.flush_interval_s = flush_interval_s
        self.max_pending_keys = 1_000
        self._pending: OrderedDict[tuple[ChatRef, str | None], _WindowBuffer] = OrderedDict()
        self._indexed_bot_ids: OrderedDict[str, OrderedDict[str, None]] = OrderedDict()
        self._bot_pending: dict[str, set[str]] = {}
        self._flush_task: asyncio.Task | None = None
        self._index_tasks: set[asyncio.Task] = set()
        self._stores: Stores | None = None

    async def purge_older_than(self, cutoff: datetime, stores: Stores) -> None:
        if stores.search is None:
            return
        chats: dict[str, ChatRef] = {}
        if stores.db is not None:
            try:
                rows = await stores.db.fetch("SELECT DISTINCT chat_key FROM messages")
                chats.update(
                    (str(row["chat_key"]), ChatRef.parse(str(row["chat_key"]))) for row in rows
                )
            except Exception as exc:
                logger.warning(
                    "search retention chat lookup failed error_class=%s", type(exc).__name__
                )
        chats.update((chat.key, chat) for chat, _thread in self._pending)
        for chat in chats.values():
            try:
                await stores.search.delete(
                    INDEX,
                    chat=chat,
                    query={"range": {"ts_end": {"lt": cutoff.isoformat()}}},
                )
            except Exception as exc:
                logger.warning(
                    "search retention delete failed chat=%s error_class=%s",
                    chat.key,
                    type(exc).__name__,
                )

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "read_chat_history",
                "In a group, search this chat. In a DM, search allowlisted groups you belong to, "
                "not the DM itself. "
                "Or page a current-chat message by message_id/offset; follow next_offset. "
                "Excerpts aren't document limits. "
                "Not for saved facts → recall; not for money → list_expenses/get_balances.",
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "description": "Search text; omit when paging message_id.",
                            "type": "string",
                        },
                        "message_id": {
                            "description": "Exact current-chat/topic message ID; omit query.",
                            "type": "string",
                        },
                        "offset": {
                            "description": "Text character offset for message_id; use next_offset.",
                            "type": "integer",
                            "minimum": 0,
                            "default": 0,
                        },
                        "from_user": {
                            "description": "@username, display name, or platform user ID.",
                            "type": ["string", "null"],
                        },
                        "since": {
                            "description": "Earliest date (YYYY-MM-DD) or ISO datetime.",
                            "type": ["string", "null"],
                        },
                        "limit": {
                            "description": "Result count; default and maximum are 8.",
                            "type": "integer",
                            "default": 8,
                            "minimum": 1,
                            "maximum": 8,
                        },
                        "include_bot_answers": {
                            "description": "Include bot messages; defaults to false.",
                            "type": "boolean",
                            "default": False,
                        },
                    },
                },
                self.read_chat_history,
            ),
            Tool(
                "read_attachment",
                "Read a shared text file or PDF, or an earlier message's image. Not for images you "
                "can already see; receipts to record → read_receipt.",
                {
                    "type": "object",
                    "properties": {
                        "message_id": {
                            "description": "Transcript message ID; defaults to latest or reply.",
                            "type": ["string", "null"],
                        },
                        "index": {
                            "description": "Attachment index in that message; defaults to 0.",
                            "type": "integer",
                            "default": 0,
                            "minimum": 0,
                        },
                    },
                },
                self.read_attachment,
            ),
        ]

    def commands(self) -> list[Command]:
        async def admin_search(ctx: CommandContext, query: str) -> str | CommandReply | None:
            if not query.strip():
                return CommandReply(
                    "Reply to this message with the text to search for.",
                    awaits_reply=True,
                    placeholder="Search text",
                )
            tool_ctx = ToolContext(
                chat=ctx.chat,
                user=ctx.user,
                is_dm=ctx.is_dm,
                platform=ctx.platform,
                stores=ctx.stores,
                is_admin=ctx.is_admin,
            )
            output = await self.debug_search(tool_ctx, query.strip())
            return output.content

        return [Command("search", "Show raw hybrid search ranks", admin_search, admin_only=True)]

    async def on_startup(self, stores: Stores) -> None:
        self._stores = stores
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval_s)
            await self._flush_pending()

    async def on_message(self, message: IncomingMessage, stores: Stores) -> None:
        self._stores = stores
        key = (message.chat, message.thread_id)
        buffer = self._pending.get(key)
        if buffer is None:
            if len(self._pending) >= self.max_pending_keys:
                self._pending.popitem(last=False)
            buffer = _WindowBuffer()
            self._pending[key] = buffer
        else:
            self._pending.move_to_end(key)
        buffer.dirty = True
        window_filled = False
        if not message.user.is_bot:
            buffer.messages.append(message)
            buffer.partial_indexed = False
            stride = max(1, self.settings.window_size - 1)
            while len(buffer.messages) >= self.settings.window_size:
                self._schedule(
                    self._index_window(buffer.messages[: self.settings.window_size], stores)
                )
                del buffer.messages[:stride]
                # The retained overlap is already present in the just-indexed full window.
                buffer.partial_indexed = bool(buffer.messages)
                window_filled = True
        if len(buffer.messages) == 1 or window_filled:
            self._schedule_bot_scan(message.chat, message.thread_id, stores)

    def _schedule(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.create_task(coroutine)
        self._index_tasks.add(task)
        task.add_done_callback(self._index_tasks.discard)

    def _schedule_bot_scan(self, chat: ChatRef, thread_id: str | None, stores: Stores) -> None:
        self._schedule(self._index_bot_answers(chat, thread_id, stores))

    async def _flush_pending(self) -> None:
        if self._stores is None:
            return
        for (chat, thread_id), buffer in self._pending.items():
            if not buffer.dirty:
                continue
            if buffer.messages and not buffer.partial_indexed:
                self._schedule(self._index_window(list(buffer.messages), self._stores))
                buffer.partial_indexed = True
            self._schedule_bot_scan(chat, thread_id, self._stores)
            buffer.dirty = False

    @staticmethod
    def _window_doc_id(messages: list[IncomingMessage]) -> str:
        first = messages[0]
        thread = first.thread_id or "-"
        return f"{_WINDOW_PREFIX}{first.chat.key}:{thread}:{first.message_id}"

    async def _index_window(self, messages: list[IncomingMessage], stores: Stores) -> None:
        if not messages or stores.search is None:
            return
        try:
            text = "\n".join(f"{m.user.display_name}: {m.text}" for m in messages if m.text)
            if not text:
                return
            vector = await self.embedder.embed_documents([text])
            await stores.search.index(
                INDEX,
                self._window_doc_id(messages),
                {
                    "kind": "message_window",
                    "user_keys": list(dict.fromkeys(m.user.ref.key for m in messages)),
                    "ts_start": messages[0].sent_at.isoformat(),
                    "ts_end": messages[-1].sent_at.isoformat(),
                    "thread_id": messages[0].thread_id,
                    "message_ids": [m.message_id for m in messages],
                    "text": text,
                    "embedding": vector[0],
                },
                chat=messages[0].chat,
            )
        except Exception as exc:
            logger.warning("search index failed error_class=%s", type(exc).__name__)

    async def _index_bot_answers(
        self, chat: ChatRef, thread_id: str | None, stores: Stores
    ) -> None:
        if stores.search is None:
            return
        try:
            messages = await stores.repo.recent_messages(
                chat, limit=2 * self.settings.window_size, thread_id=thread_id
            )
        except Exception as exc:
            logger.warning("bot message lookup failed error_class=%s", type(exc).__name__)
            return
        if chat.key not in self._indexed_bot_ids:
            while len(self._indexed_bot_ids) >= self.max_pending_keys:
                expired_chat, _ = self._indexed_bot_ids.popitem(last=False)
                self._bot_pending.pop(expired_chat, None)
            self._indexed_bot_ids[chat.key] = OrderedDict()
            self._bot_pending[chat.key] = set()
        else:
            self._indexed_bot_ids.move_to_end(chat.key)
        indexed = self._indexed_bot_ids[chat.key]
        pending = self._bot_pending[chat.key]
        for message in messages:
            if not message.is_bot or message.message_id in indexed or message.message_id in pending:
                continue
            pending.add(message.message_id)
            try:
                vector = await self.embedder.embed_documents([message.text])
                await stores.search.index(
                    INDEX,
                    f"{_BOT_PREFIX}{chat.key}:{message.message_id}",
                    {
                        "kind": "bot_answer",
                        "user_keys": [message.user.key],
                        "ts_start": message.sent_at.isoformat(),
                        "ts_end": message.sent_at.isoformat(),
                        "thread_id": message.thread_id,
                        "message_ids": [message.message_id],
                        "text": message.text,
                        "embedding": vector[0],
                    },
                    chat=chat,
                )
                indexed[message.message_id] = None
                indexed.move_to_end(message.message_id)
                while len(indexed) > _BOT_ID_CACHE_SIZE:
                    indexed.popitem(last=False)
            except Exception as exc:
                logger.warning("bot message index failed error_class=%s", type(exc).__name__)
            finally:
                pending.discard(message.message_id)

    async def read_chat_history(
        self,
        ctx: ToolContext,
        query: str = "",
        from_user: str | None = None,
        since: str | None = None,
        limit: int = 8,
        include_bot_answers: bool = False,
        message_id: str | None = None,
        offset: int = 0,
    ) -> ToolResult:
        if type(offset) is not int or offset < 0:
            return ToolResult.error("offset must be a nonnegative integer.")
        if message_id is not None:
            if query or from_user or since:
                return ToolResult.error("Use message_id and offset without search filters.")
            if not isinstance(message_id, str) or not 1 <= len(message_id) <= 128:
                return ToolResult.error("Provide a valid current-chat message_id.")
            source = await ctx.stores.repo.get_message(ctx.chat, message_id)
            if (
                source is None
                or source.message_id != message_id
                or source.chat != ctx.chat
                or source.thread_id != ctx.thread_id
            ):
                return ToolResult.error("History message not found in this chat and topic.")
            if source.is_bot and include_bot_answers is not True:
                return ToolResult.error(
                    "Set include_bot_answers=true to page an assistant message."
                )
            try:
                content = render_history_page(message_record(source), offset=offset)
            except ValueError as exc:
                return ToolResult.error(str(exc))
            return ToolResult(content, untrusted=True, persist_content=False)
        if offset:
            return ToolResult.error("offset requires a message_id.")
        if not isinstance(query, str) or not query.strip():
            return ToolResult.error("Provide a search query or an exact message_id.")
        try:
            since_at = self._parse_since(since) if since else None
        except ValueError:
            return ToolResult.error("since must be an ISO date like 2026-10-01")
        limit = max(1, min(limit, 8))
        chats = [ctx.chat]
        if ctx.is_dm:
            if self.member_groups is None:
                return ToolResult.error(
                    "Search unavailable in DMs until membership lookup is configured."
                )
            chats = await self.member_groups(ctx.user.ref)
            if not chats:
                if from_user:
                    return ToolResult.error("No member matches from_user")
                return ToolResult("No group history is available for this user.")

        results: list[tuple[ChatRef, SearchHit]] = []
        matched_user = False
        for chat in chats:
            user_keys = await self._resolve_from_user(ctx, chat, from_user) if from_user else None
            if from_user and not user_keys:
                continue
            matched_user |= user_keys is not None
            hits = await self._search_chat(
                ctx, chat, query, user_keys, since_at, limit, include_bot_answers
            )
            if hits is None:
                return ToolResult.error("Chat history search is unavailable.")
            results.extend((chat, hit) for hit in hits)
        if from_user and not matched_user:
            return ToolResult.error("No member matches from_user")

        results.sort(key=lambda item: item[1].score, reverse=True)
        snippets: list[str] = []
        for chat, hit in results[:limit]:
            messages = []
            for message_id in hit.source.get("message_ids", []):
                message = await ctx.stores.repo.get_message(chat, message_id)
                if message:
                    messages.append(message)
            if not messages:
                snippets.append(
                    render_history_page(
                        {
                            "message_id": None,
                            "role": "unknown",
                            "speaker": {
                                "user_key": None,
                                "display_name": "unknown speaker",
                                "username": None,
                            },
                            "reply_to_id": None,
                            "mentions": [],
                            "text": str(hit.source.get("text", "")),
                        },
                    )
                )
            else:
                snippets.extend(
                    f"[{message.sent_at.isoformat()}] "
                    + render_history_page(message_record(message))
                    for message in messages
                )
        if not snippets:
            return ToolResult("No matching chat history was found.", untrusted=True)
        return ToolResult("\n".join(dict.fromkeys(snippets)), untrusted=True, persist_content=False)

    @staticmethod
    def _parse_since(value: str) -> datetime:
        try:
            if len(value) == 10:
                parsed: datetime = datetime.combine(date.fromisoformat(value), datetime.min.time())
            else:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid ISO date or datetime") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    async def _resolve_from_user(self, ctx: ToolContext, chat: ChatRef, query: str) -> list[str]:
        candidate = query.strip().lstrip("@").casefold()
        matches: list[str] = []
        for member in await ctx.stores.repo.find_members(chat):
            aliases = {
                member.ref.key.casefold(),
                member.ref.id.casefold(),
                (member.username or "").lstrip("@").casefold(),
                member.display_name.casefold(),
            }
            if candidate in aliases and member.ref.key not in matches:
                matches.append(member.ref.key)
        return matches

    async def _search_chat(
        self,
        ctx: ToolContext,
        chat: ChatRef,
        query: str,
        user_keys: list[str] | None,
        since: datetime | None,
        limit: int,
        include_bot_answers: bool = False,
    ) -> list[SearchHit] | None:
        filters: dict[str, Any] = {}
        if not include_bot_answers:
            filters["kind"] = {"not": "bot_answer"}
        if user_keys:
            filters["user_keys"] = user_keys
        if since:
            filters["ts_end"] = {"gte": since.isoformat()}
        if ctx.stores.search is not None:
            try:
                vector = await self.embedder.embed_query(query)
                return await ctx.stores.search.search(
                    INDEX, chat=chat, text=query, vector=vector, filters=filters, size=limit
                )
            except Exception as exc:
                logger.warning("search query failed error_class=%s", type(exc).__name__)
        if ctx.stores.db is None:
            return None
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = await ctx.stores.db.fetch(
            "SELECT message_id, text, sent_at, user_key FROM messages "
            "WHERE chat_key=$1 AND text ILIKE $2 ESCAPE '\\' "
            "AND ($4::text[] IS NULL OR user_key=ANY($4)) "
            "AND ($5::timestamptz IS NULL OR sent_at >= $5) "
            "AND ($6::boolean OR is_bot=false) "
            "ORDER BY sent_at DESC LIMIT $3",
            chat.key,
            f"%{escaped}%",
            limit,
            user_keys,
            since,
            include_bot_answers,
        )
        return [
            SearchHit(
                str(row.get("message_id", "")),
                float(len(rows) - index),
                {
                    "chat_key": chat.key,
                    "message_ids": [str(row.get("message_id", ""))],
                    "text": row.get("text", ""),
                },
            )
            for index, row in enumerate(rows)
        ]

    async def debug_search(self, ctx: ToolContext, query: str) -> ToolResult:
        if ctx.stores.search is None:
            return ToolResult("Search index unavailable.")
        vector = await self.embedder.embed_query(query)
        bm25, knn = await asyncio.gather(
            ctx.stores.search.search(INDEX, chat=ctx.chat, text=query, size=8),
            ctx.stores.search.search(INDEX, chat=ctx.chat, vector=vector, size=8),
        )
        bm25_rank = {hit.doc_id: rank for rank, hit in enumerate(bm25, 1)}
        vector_rank = {hit.doc_id: rank for rank, hit in enumerate(knn, 1)}
        from chatcore.store.es import rrf_fuse

        fused = rrf_fuse([[hit.doc_id for hit in bm25], [hit.doc_id for hit in knn]])
        fused_rank = {doc_id: rank for rank, (doc_id, _) in enumerate(fused, 1)}
        ids = dict.fromkeys([*bm25_rank, *vector_rank])
        output = ["doc_id | BM25 | vector | fused"]
        output.extend(
            "{} | {} | {} | {}".format(
                doc_id,
                bm25_rank.get(doc_id, "—"),
                vector_rank.get(doc_id, "—"),
                fused_rank[doc_id],
            )
            for doc_id in ids
        )
        return ToolResult("\n".join(output))

    async def read_attachment(
        self, ctx: ToolContext, message_id: str | None = None, index: int = 0
    ) -> ToolResult:
        if not ctx.capabilities.files:
            return ToolResult.error("attachment unavailable: platform cannot download files")
        stored: StoredMessage | None
        if message_id:
            stored = await ctx.stores.repo.get_message(ctx.chat, message_id)
            if stored is None:
                return ToolResult.error("attachment unavailable: message not found in this chat")
            if ctx.thread_id is not None and stored.thread_id != ctx.thread_id:
                return ToolResult.error("attachment unavailable: message is in a different topic")
            source_id = stored.message_id
        else:
            recent = await ctx.stores.repo.recent_messages(
                ctx.chat, limit=20, thread_id=ctx.thread_id
            )
            stored = max(
                (
                    (position, msg)
                    for position, msg in enumerate(recent)
                    if msg.user == ctx.user.ref
                ),
                key=lambda item: (item[1].sent_at, item[0]),
                default=(0, None),
            )[1]
            if stored is None:
                return ToolResult.error("attachment unavailable: no recent message from you")
            source_id = stored.message_id
            if not stored.attachments and stored.reply_to_id:
                stored = await ctx.stores.repo.get_message(ctx.chat, stored.reply_to_id)
                if stored is None:
                    return ToolResult.error("attachment unavailable: replied-to message not found")
                source_id = stored.message_id
        attachments = stored.attachments
        if index < 0 or index >= len(attachments):
            return ToolResult.error(
                f"attachment unavailable: message has no attachment at index {index}"
            )
        attachment = attachments[index]
        if (
            attachment.size_bytes is not None
            and attachment.size_bytes > self.settings.attachment_max_bytes
        ):
            return ToolResult.error("attachment unavailable: exceeds the size limit")
        if is_image(attachment):
            if not ctx.turn.vision and ctx.turn.image_reader is not None:
                try:
                    image_part = await ctx.turn.image_reader.load(
                        ctx.platform, attachment, chat=ctx.chat, cache=ctx.turn.downloads
                    )
                except ImageUnavailable as exc:
                    reason = {
                        "download_failed": "download failed, please try again",
                        "too_large": "exceeds the size limit",
                        "unsupported_format": "not JPEG/PNG/WebP",
                    }[exc.reason]
                    return ToolResult.error(f"attachment unavailable: {reason}")
                except Exception:
                    return ToolResult.error("attachment unavailable: image description failed")
                reason = await admit_describe(
                    ctx, ctx.turn.limits, ctx.turn.describe_per_user_per_day
                )
                if reason:
                    if reason == "image limit for this turn reached":
                        return ToolResult.error(f"attachment unavailable: {reason}")
                    if reason == "daily image limit reached":
                        return ToolResult.error(
                            "attachment unavailable: daily image limit reached (resets tomorrow)"
                        )
                    return ToolResult.error(
                        "attachment unavailable: the bot has reached today's token budget"
                    )
                ctx.turn.images_remaining -= 1
                try:
                    reading = await ctx.turn.image_reader.describe(
                        image_part,
                        session_key=ctx.chat.key + (":" + ctx.thread_id if ctx.thread_id else ""),
                        chat_key=ctx.chat.key,
                        cache=None,
                    )
                except ImageReadError as exc:
                    if ctx.turn.record_usage is not None:
                        await ctx.turn.record_usage(exc.usage)
                    return ToolResult.error("attachment unavailable: image description failed")
                except Exception:
                    return ToolResult.error("attachment unavailable: image description failed")
                if ctx.turn.record_usage is not None:
                    await ctx.turn.record_usage(reading.usage)
                return ToolResult(
                    f"Description of the image from message {source_id}, written by a separate "
                    f"vision model (untrusted data):\n{reading.text}",
                    untrusted=True,
                    persist_content=False,
                )
            if not ctx.turn.vision:
                return ToolResult.error("attachment unavailable: image viewing is not enabled")
            if ctx.turn.images_remaining <= 0:
                return ToolResult.error("attachment unavailable: image limit for this turn reached")
            try:
                image_part = await asyncio.wait_for(
                    download_image(
                        ctx.platform, attachment, chat=ctx.chat, cache=ctx.turn.downloads
                    ),
                    timeout=self.settings.parse_timeout_s,
                )
            except TimeoutError:
                return ToolResult.error("attachment unavailable: download or parse timed out")
            except ImageUnavailable as exc:
                reason = {
                    "download_failed": "download failed, please try again",
                    "too_large": "exceeds the size limit",
                    "unsupported_format": "not JPEG/PNG/WebP",
                }[exc.reason]
                return ToolResult.error(f"attachment unavailable: {reason}")
            ctx.turn.images_remaining -= 1
            return ToolResult(
                f"Image from message {source_id} attached for you to view (untrusted data).",
                untrusted=True,
                images=(image_part,),
            )
        if not self._is_supported(attachment):
            return ToolResult.error("attachment unavailable: unsupported type")
        data = ctx.turn.downloads.get(attachment.id)
        if data is None:
            try:
                data = await asyncio.wait_for(
                    ctx.platform.download(
                        attachment, chat=ctx.chat, max_bytes=self.settings.attachment_max_bytes
                    ),
                    timeout=self.settings.parse_timeout_s,
                )
            except TimeoutError:
                return ToolResult.error("attachment unavailable: download or parse timed out")
            except PlatformError:
                return ToolResult.error("attachment unavailable: download failed, please try again")
            except Exception:
                return ToolResult.error("attachment unavailable: download failed, please try again")
            ctx.turn.downloads[attachment.id] = data
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(self._parse_attachment, attachment, data),
                timeout=self.settings.parse_timeout_s,
            )
        except TimeoutError:
            return ToolResult.error("attachment unavailable: download or parse timed out")
        except Exception as exc:
            logger.warning("attachment parse failed error_class=%s", type(exc).__name__)
            if isinstance(exc, ValueError) and (
                (attachment.filename or "").casefold().endswith(".csv")
                or (attachment.mime_type or "").casefold() == "text/csv"
            ):
                return ToolResult.error("attachment unavailable: " + str(exc))
            return ToolResult.error(
                f"attachment unavailable: could not read file ({type(exc).__name__})"
            )
        text = text[:50_000]
        if not text.strip():
            return ToolResult.error("attachment unavailable: no extractable text")
        self._schedule(self._index_attachment(ctx, attachment, source_id, stored.user.key, text))
        return ToolResult(text, untrusted=True)

    @staticmethod
    def _is_supported(attachment: Attachment) -> bool:
        filename = (attachment.filename or "").casefold()
        mime = (attachment.mime_type or "").casefold()
        return (
            filename.endswith(".pdf")
            or mime == "application/pdf"
            or filename.endswith((".txt", ".md", ".csv"))
            or mime.startswith("text/")
        )

    def _parse_attachment(self, attachment: Attachment, data: bytes) -> str:
        if len(data) > self.settings.attachment_max_bytes:
            raise ValueError("attachment exceeds configured size limit")
        filename = (attachment.filename or "").casefold()
        mime = (attachment.mime_type or "").casefold()
        if filename.endswith(".csv") or mime == "text/csv":
            return parse_csv(
                data,
                max_bytes=self.settings.csv_max_bytes,
                max_rows=self.settings.csv_max_rows,
                max_columns=self.settings.csv_max_columns,
                max_field_chars=self.settings.csv_max_field_chars,
            ).render()
        if filename.endswith(".pdf") or mime == "application/pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            return "\n".join(
                (page.extract_text() or "") for page in reader.pages[: self.settings.pdf_max_pages]
            )
        if filename.endswith((".txt", ".md", ".csv")) or mime.startswith("text/"):
            return data.decode("utf-8", errors="replace")
        raise ValueError("Unsupported attachment type.")

    @staticmethod
    def _chunks(text: str) -> list[str]:
        step = _CHUNK_SIZE - _CHUNK_OVERLAP
        return [text[start : start + _CHUNK_SIZE] for start in range(0, len(text), step)]

    async def _index_attachment(
        self,
        ctx: ToolContext,
        attachment: Attachment,
        message_id: str,
        sender_key: str,
        text: str,
    ) -> None:
        if ctx.stores.search is None or not text:
            return
        try:
            chunks = self._chunks(text)
            vectors = await self.embedder.embed_documents(chunks)
            for number, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                await ctx.stores.search.index(
                    INDEX,
                    f"attachment:{ctx.chat.key}:{message_id}:{attachment.id}:{number}",
                    {
                        "kind": "attachment",
                        "user_keys": [sender_key],
                        "file_id": attachment.id,
                        "message_ids": [message_id],
                        "text": chunk,
                        "embedding": vector,
                    },
                    chat=ctx.chat,
                )
        except Exception as exc:
            logger.warning("attachment index failed error_class=%s", type(exc).__name__)

    async def purge_user(self, chat: ChatRef, user: UserRef, stores: Stores) -> None:
        for key, buffer in list(self._pending.items()):
            if key[0] != chat:
                continue
            removed_authored = any(message.user.ref == user for message in buffer.messages)
            remaining = [
                replace(message, mentions=())
                for message in buffer.messages
                if message.user.ref != user
            ]
            if not removed_authored and remaining == buffer.messages:
                continue
            buffer.messages = remaining
            if removed_authored:
                buffer.partial_indexed = False
                buffer.dirty = True
            if not remaining:
                del self._pending[key]
        if stores.search:
            await stores.search.delete(INDEX, chat=chat, query={"term": {"user_keys": user.key}})

    async def purge_chat(self, chat: ChatRef, stores: Stores) -> None:
        for key in [key for key in self._pending if key[0] == chat]:
            del self._pending[key]
        self._indexed_bot_ids.pop(chat.key, None)
        self._bot_pending.pop(chat.key, None)
        if stores.search:
            await stores.search.delete(INDEX, chat=chat)
