"""Per-chat durable facts with optional vector recall and conservative extraction."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping
from typing import Any

from chatcore.command_prompts import CommandReply
from chatcore.embed import Embedder
from chatcore.events import ChatRef, IncomingMessage, UserRef
from chatcore.limits import Limits
from chatcore.llm.base import Provider
from chatcore.llm.types import Message
from chatcore.pending import PendingAction, PendingActionHandler
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.skills import Skill
from chatcore.store.base import SearchHit, Stores
from chatcore.tools.registry import Tool, ToolContext, ToolResult
from pydantic_settings import BaseSettings, SettingsConfigDict

from .repository import MemoryMemoryRepo, MemoryRepo, PostgresMemoryRepo

logger = logging.getLogger(__name__)

SENSITIVE_PATTERNS = (
    re.compile(
        r"\b(?:health|medical|diagnos(?:is|ed)|illness|disease|allerg(?:y|ic)|medication|prescription|surgery|pregnan\w*)\b",
        re.I,
    ),
    re.compile(
        r"(?:\$|S\$|€|£|¥|\b(?:USD|SGD|EUR|GBP|dollars?|cash|salary|income|bank balance)\s*)\s*\d",
        re.I,
    ),
    re.compile(
        r"\b(?:account|card|credit card|debit card|routing)\s*"
        r"(?:number|no\.?|#)?\s*[:#-]?\s*\d[\d -]{5,}\b",
        re.I,
    ),
    re.compile(r"\b(?:password|passcode|pin|otp|one[- ]time code|api key|secret key)\b", re.I),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(
        r"(?<!\w)(?!\d{4}-\d{2}-\d{2}\b)(?=(?:[\d ()+-]*\d){8})"
        r"\+?\d[\d ()-]*\d(?!\w)"
    ),
    re.compile(r"\b[STFG]\d{7}[A-Z]\b", re.I),
    re.compile(
        r"\b\d{1,6}\s+[\w .'-]+\s+(?:street|st\.?|road|rd\.?|avenue|ave\.?|drive|dr\.?|"
        r"lane|ln\.?|boulevard|blvd\.?)\b",
        re.I,
    ),
)

EXTRACTION_SYSTEM = """Extract at most two durable facts about the group and its people from a
human message. Include standing preferences, plans, or arrangements only. Do not extract opinions,
one-off chatter, questions, or anything sensitive. Return only JSON with shape
{"facts":[{"text":"...","about_speaker":false}]}. The facts array may be empty. Never infer
information that was not directly stated."""


# Default behaviour text for the `memory` skill; apps may add to or override it.
MEMORY_INSTRUCTIONS = (
    "Use remember for durable facts people explicitly ask you to keep. Forget an old fact "
    "before saving a contradiction. Remember and forget apply immediately with an echo. "
    "For undo, forget the echoed ID after a save, or remember the echoed text after a "
    "forget or wording update. Use recall to find saved facts. Don't save time-zone "
    "preferences as memories; use the time-zone setting."
)


def _parse_extraction_response(response_text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Find exactly one complete facts object in model output without trusting surrounding prose."""
    response_text = response_text.strip()
    if not response_text:
        return None, "empty"

    decoder = json.JSONDecoder()
    objects: list[tuple[Any, int, int]] = []
    errors: list[tuple[int, json.JSONDecodeError]] = []
    offset = 0
    while offset < len(response_text):
        if response_text[offset] not in "[{":
            offset += 1
            continue
        stack: list[str] = []
        in_string = False
        escaped = False
        for char in response_text[:offset]:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "[{":
                stack.append(char)
            elif char in "]}" and stack:
                stack.pop()
        if stack:
            offset += 1
            continue
        try:
            value, end = decoder.raw_decode(response_text, offset)
        except json.JSONDecodeError as exc:
            errors.append((offset, exc))
            offset += 1
            continue
        objects.append((value, offset, end))
        offset = end

    facts_objects = [
        item
        for item in objects
        if isinstance(item[0], dict) and isinstance(item[0].get("facts"), list)
    ]
    if len(objects) == 1 and len(facts_objects) == 1:
        value, _start, _end = facts_objects[0]
        return value, None
    if any(
        error.pos >= len(response_text) - 1 or error.msg.startswith("Unterminated string")
        for _start, error in errors
    ):
        return None, "truncated"
    if objects:
        return None, "wrong_schema"
    return None, "invalid_json"


class MemorySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMORY_", env_file=None, extra="ignore")

    recall_k: int = 5
    pinned_max: int = 10
    auto_extract: bool = True
    dedupe_threshold: float = 0.92
    max_chars: int = 300


class MemoryPlugin(BasePlugin):
    name = "memory"
    help_title = "Memory"

    def skills(self):
        return [
            Skill(
                "memory",
                "Save, find and remove durable memories",
                (
                    "remember this",
                    "what did I say about",
                    "记住这件事",
                    "忘掉这条记忆",
                    "can recall this anot",
                ),
                ("remember", "forget", "recall"),
                keywords=("remember", "memory", "记住", "记忆"),
                max_steps=3,
                instructions=MEMORY_INSTRUCTIONS,
            )
        ]

    migrations_package = "chatcore_memory.migrations"

    def __init__(
        self,
        embedder: Embedder | None = None,
        *,
        provider: Provider | None = None,
        limits: Limits | None = None,
        settings: MemorySettings | None = None,
    ) -> None:
        self.embedder = embedder
        self.provider = provider
        self.limits = limits
        self.settings = settings or MemorySettings()
        self._memory_repo: MemoryMemoryRepo | None = None

    def _repo(self, stores: Stores) -> MemoryRepo:
        if stores.db is not None:
            return PostgresMemoryRepo(stores.db)
        if self._memory_repo is None:
            self._memory_repo = MemoryMemoryRepo()
        return self._memory_repo

    async def _embed(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        return await self.embedder.embed_query(text)

    async def _index(self, chat: ChatRef, row: Mapping[str, Any], stores: Stores) -> None:
        if stores.search is None or self.embedder is None:
            return
        vector = await self.embedder.embed_documents([str(row["text"])])
        await stores.search.index(
            "memories",
            str(row["id"]),
            {
                "memory_id": int(row["id"]),
                "text": row["text"],
                "subject_user_key": row.get("subject_user_key"),
                "source": row["source"],
                "pinned": bool(row["pinned"]),
                "created_at": row["created_at"],
                "embedding": vector[0],
            },
            chat=chat,
        )

    async def _delete_index(self, chat: ChatRef, memory_ids: list[int], stores: Stores) -> None:
        if stores.search is None:
            return
        for memory_id in memory_ids:
            await stores.search.delete(
                "memories", chat=chat, query={"term": {"memory_id": memory_id}}
            )

    @staticmethod
    def _jaccard(left: str, right: str) -> float:
        def tokenize(value: str) -> set[str]:
            return set(re.findall(r"\w+", value.casefold()))

        a, b = tokenize(left), tokenize(right)
        return len(a & b) / len(a | b) if a or b else 1.0

    async def _similar(self, chat: ChatRef, text: str, stores: Stores) -> dict[str, Any] | None:
        repo = self._repo(stores)
        if stores.search is not None and self.embedder is not None:
            try:
                vector = (await self.embedder.embed_documents([text]))[0]
                hits = await stores.search.search("memories", chat=chat, vector=vector, size=5)
                for hit in hits:
                    old_vector = hit.source.get("embedding")
                    score = self._cosine(vector, old_vector) if old_vector else None
                    if score is not None and score >= self.settings.dedupe_threshold:
                        found = await repo.get(chat, int(hit.source.get("memory_id", hit.doc_id)))
                        if found:
                            return found
            except Exception as exc:
                logger.warning("memory vector dedupe failed error=%s", type(exc).__name__)
        for row in await repo.list(chat, limit=500):
            if self._jaccard(text, str(row["text"])) >= 0.8:
                return row
        return None

    @staticmethod
    def _cosine(left: list[float] | None, right: Any) -> float | None:
        if left is None or not isinstance(right, (tuple, list)) or len(left) != len(right):
            return None
        numerator = sum(a * float(b) for a, b in zip(left, right, strict=True))
        denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(
            sum(float(b) * float(b) for b in right)
        )
        return numerator / denominator if denominator else None

    async def _save(
        self,
        chat: ChatRef,
        text: str,
        stores: Stores,
        *,
        source: str,
        pinned: bool,
        created_by: UserRef,
        subject_user: UserRef | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        repo = self._repo(stores)
        existing = await self._similar(chat, text, stores)
        if existing:
            row = await repo.update(chat, int(existing["id"]), text)
            updated = row is not None
            previous_text = str(existing["text"]) if updated else None
        else:
            row = await repo.insert(
                chat,
                text,
                source=source,
                pinned=pinned,
                created_by=created_by,
                subject_user=subject_user,
            )
            updated = False
            previous_text = None
        assert row is not None
        try:
            await self._index(chat, row, stores)
        except Exception as exc:
            logger.warning(
                "memory index write failed chat=%s id=%s error=%s",
                chat.key,
                row["id"],
                type(exc).__name__,
            )
        logger.info("memory saved chat=%s id=%s updated=%s", chat.key, row["id"], updated)
        return row, previous_text

    async def _resolve_subject(self, ctx: ToolContext, about: str | None):
        if not about:
            return None
        search = about.lstrip("@").strip().casefold()
        try:
            members = await ctx.stores.repo.find_members(ctx.chat)
        except Exception:
            return None
        exact = [
            member
            for member in members
            if (member.username and member.username.casefold() == search)
            or member.display_name.casefold() == search
        ]
        unique = {member.ref.key: member for member in exact}
        return next(iter(unique.values())).ref if len(unique) == 1 else None

    def _pending(self, ctx: ToolContext, kind: str, payload: dict, summary: str) -> ToolResult:
        action = PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin=self.name,
            kind=kind,
            payload=payload,
            summary=summary,
            thread_id=ctx.thread_id,
        )
        return ToolResult(content=summary, pending_action=action)

    async def _remember(
        self,
        ctx: ToolContext,
        fact: str,
        about: str | None = None,
        pinned: bool = False,
        *,
        source: str = "explicit",
        created_by: UserRef | None = None,
    ) -> ToolResult:
        fact = fact.strip()
        if not fact or len(fact) > self.settings.max_chars:
            return ToolResult.error(f"Fact must contain 1 to {self.settings.max_chars} characters.")
        if source == "explicit":
            return self._pending(
                ctx,
                "remember",
                {"fact": fact, "about": about, "pinned": pinned},
                f'Save this memory exactly as written: "{fact}"',
            )
        subject = await self._resolve_subject(ctx, about)
        row, previous_text = await self._save(
            ctx.chat,
            fact,
            ctx.stores,
            source=source,
            pinned=pinned,
            created_by=created_by or ctx.user.ref,
            subject_user=subject,
        )
        if previous_text is not None:
            return ToolResult(content=f"updated #{row['id']}: {fact}")
        return ToolResult(content=f"Saved memory #{row['id']}: {fact}")

    async def _remember_tool(
        self, ctx: ToolContext, *, fact: str, about: str | None = None, pinned: bool = False
    ) -> ToolResult:
        return await self._remember(ctx, fact, about, pinned)

    async def _recall_rows(
        self, chat: ChatRef, stores: Stores, query: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        repo = self._repo(stores)
        size = max(1, min(limit, 20))
        rows = []
        if stores.search is not None and self.embedder is not None:
            try:
                vector = await self._embed(query)
                hits: list[SearchHit] = await stores.search.search(
                    "memories", chat=chat, text=query, vector=vector, size=size
                )
                for hit in hits:
                    row = await repo.get(chat, int(hit.source.get("memory_id", hit.doc_id)))
                    if row:
                        rows.append(row)
            except Exception as exc:
                logger.warning(
                    "memory recall fallback chat=%s error=%s", chat.key, type(exc).__name__
                )
        if not rows:
            rows = await repo.match(chat, query, limit=size)
        if not rows:
            return []
        logger.info("memory recalled chat=%s count=%d", chat.key, len(rows))
        return rows

    async def _recall(self, ctx: ToolContext, query: str, limit: int = 5) -> ToolResult:
        rows = await self._recall_rows(ctx.chat, ctx.stores, query, limit)
        if not rows:
            return ToolResult(content="No matching memories.", untrusted=True)
        return ToolResult(
            content="\n".join(f"#{row['id']} {row['text']}" for row in rows), untrusted=True
        )

    async def _forget(self, ctx: ToolContext, memory_id: int) -> ToolResult:
        row = await self._repo(ctx.stores).get(ctx.chat, memory_id)
        if not row:
            return ToolResult.error(f"No memory #{memory_id} in this chat.")
        return self._pending(
            ctx,
            "forget",
            {"memory_id": memory_id},
            f'Delete memory #{memory_id}: "{row["text"]}"',
        )

    async def _delete_memory(self, chat: ChatRef, memory_id: int, stores: Stores) -> bool:
        row = await self._repo(stores).delete(chat, memory_id)
        if not row:
            return False
        try:
            await self._delete_index(chat, [memory_id], stores)
        except Exception as exc:
            logger.warning(
                "memory index delete failed chat=%s id=%s error=%s",
                chat.key,
                memory_id,
                type(exc).__name__,
            )
        logger.info("memory deleted chat=%s id=%s", chat.key, memory_id)
        return True

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "remember",
                "Save a durable fact the user asks to keep. Applies immediately (may ask the user "
                "to confirm after web or file content); forget undoes it.",
                {
                    "type": "object",
                    "properties": {
                        "fact": {"description": "Durable fact to save.", "type": "string"},
                        "about": {
                            "description": "Optional person or subject.",
                            "type": ["string", "null"],
                        },
                        "pinned": {
                            "description": "Pin this memory; defaults to false.",
                            "type": "boolean",
                            "default": False,
                        },
                    },
                    "required": ["fact"],
                },
                self._remember_tool,
                writes=True,
                direct=True,
            ),
            Tool(
                "recall",
                "Find saved facts relevant to a query. Not for chat messages → read_chat_history.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"description": "Search text or question.", "type": "string"},
                        "limit": {
                            "description": "Result count; default 5, range 1–20.",
                            "type": "integer",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
                self._recall,
            ),
            Tool(
                "forget",
                "Delete a saved fact. Applies immediately (may ask the user to confirm "
                "after web or file content); "
                "remember undoes it.",
                {
                    "type": "object",
                    "properties": {
                        "memory_id": {
                            "description": "Memory ID from recall or save echo.",
                            "type": "integer",
                        }
                    },
                    "required": ["memory_id"],
                },
                self._forget,
                writes=True,
                direct=True,
            ),
        ]

    def commands(self) -> list[Command]:
        async def show(ctx: CommandContext, _args: str) -> str:
            rows = await self._repo(ctx.stores).list(ctx.chat, limit=50)
            if not rows:
                return "I don't remember anything about this chat yet."
            lines = []
            for row in rows:
                marks = (" [pinned]" if row["pinned"] else "") + (
                    " [auto]" if row["source"] == "auto" else ""
                )
                lines.append(f"#{row['id']}{marks} {row['text']}")
            return "\n".join(lines)

        async def forget_command(ctx: CommandContext, args: str) -> str | CommandReply:
            if not args.strip():
                listing = await show(ctx, "")
                if not await self._repo(ctx.stores).list(ctx.chat, limit=1):
                    return listing
                return CommandReply(
                    listing + "\nReply to this message with the number of the memory to forget.",
                    awaits_reply=True,
                    placeholder="Memory number",
                )
            if not re.fullmatch(r"\s*#?\d+\s*", args):
                return CommandReply(
                    "Reply to this message with a memory number, e.g. 12.",
                    awaits_reply=True,
                    placeholder="Memory number",
                )
            memory_id = int(args.strip().lstrip("#"))
            return (
                f"Forgot memory #{memory_id}."
                if await self._delete_memory(ctx.chat, memory_id, ctx.stores)
                else "no such memory"
            )

        return [
            Command("memory", "List this chat's memories.", show),
            Command("forget", "Delete a memory by ID.", forget_command),
        ]

    def action_handlers(self) -> dict[str, PendingActionHandler]:
        return {"remember": _MemoryActionHandler(self), "forget": _MemoryActionHandler(self)}

    async def before_turn(self, message: IncomingMessage, text: str, stores: Stores) -> str | None:
        try:
            repo = self._repo(stores)
            pinned = await repo.pinned(message.chat, limit=self.settings.pinned_max)
            rows = await self._recall_rows(message.chat, stores, text, self.settings.recall_k)
            pinned_ids = {int(row["id"]) for row in pinned}
            recalled_rows = [row for row in rows if int(row["id"]) not in pinned_ids]
            sections = []
            if pinned:
                sections.append(
                    "Pinned facts about this chat\n"
                    + "\n".join(f"#{row['id']} {row['text']}" for row in pinned)
                )
            if recalled_rows:
                sections.append(
                    "Possibly relevant memories\n"
                    + "\n".join(f"#{row['id']} {row['text']}" for row in recalled_rows)
                )
            return "\n\n".join(sections) or None
        except Exception as exc:
            logger.warning(
                "memory before-turn failed chat=%s error=%s", message.chat.key, type(exc).__name__
            )
            return None

    async def after_turn(self, message: IncomingMessage, text: str, stores: Stores) -> None:
        if (
            self.provider is None
            or not self.settings.auto_extract
            or message.user.is_bot
            or len(text.strip()) < 12
            or text.lstrip().startswith("/")
        ):
            return
        try:
            if self.limits and await self.limits.budget_exhausted():
                return
            completion = await self.provider.complete(
                [Message.user(text)],
                system=EXTRACTION_SYSTEM,
                tools=(),
                max_tokens=200,
                session_key=message.chat.key,
            )
            if self.limits:
                await self.limits.record(
                    completion.usage.input_tokens + completion.usage.output_tokens
                )
            if completion.stop_reason == "max_tokens":
                logger.warning(
                    "memory extraction skipped chat=%s reason=max_tokens", message.chat.key
                )
                return
            data, reason = _parse_extraction_response(completion.message.text)
            if reason:
                logger.warning(
                    "memory extraction skipped chat=%s reason=%s", message.chat.key, reason
                )
                return
            assert data is not None
            facts = data["facts"]
            for fact in facts[:2]:
                if not isinstance(fact, dict) or not isinstance(fact.get("text"), str):
                    continue
                fact_text = fact["text"].strip()
                if not fact_text or len(fact_text) > self.settings.max_chars:
                    continue
                if any(pattern.search(fact_text) for pattern in SENSITIVE_PATTERNS):
                    continue
                speaker = message.user.ref if fact.get("about_speaker") is True else None
                await self._save(
                    message.chat,
                    fact_text,
                    stores,
                    source="auto",
                    pinned=False,
                    created_by=message.user.ref,
                    subject_user=speaker,
                )
        except Exception as exc:
            logger.warning(
                "memory extraction failed chat=%s error=%s", message.chat.key, type(exc).__name__
            )

    async def purge_user(self, chat: ChatRef, user: UserRef, stores: Stores) -> None:
        ids = await self._repo(stores).purge_user(chat, user)
        try:
            await self._delete_index(chat, ids, stores)
        except Exception as exc:
            logger.warning(
                "memory purge-user index failed chat=%s count=%d error=%s",
                chat.key,
                len(ids),
                type(exc).__name__,
            )

    async def purge_chat(self, chat: ChatRef, stores: Stores) -> None:
        ids = await self._repo(stores).purge_chat(chat)
        try:
            if stores.search is not None:
                await stores.search.delete("memories", chat=chat)
        except Exception as exc:
            logger.warning(
                "memory purge-chat index failed chat=%s count=%d error=%s",
                chat.key,
                len(ids),
                type(exc).__name__,
            )


class _MemoryActionHandler(PendingActionHandler):
    def __init__(self, plugin: MemoryPlugin) -> None:
        self.plugin = plugin

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        if action.kind == "remember":
            data = action.payload
            tool_ctx = ToolContext(ctx.chat, ctx.user, ctx.is_dm, ctx.platform, ctx.stores)
            subject = await self.plugin._resolve_subject(tool_ctx, data.get("about"))
            row, old_text = await self.plugin._save(
                ctx.chat,
                data["fact"],
                ctx.stores,
                source="explicit",
                pinned=bool(data.get("pinned", False)),
                created_by=ctx.user.ref,
                subject_user=subject,
            )
            if old_text is not None:
                return (
                    f'Updated memory #{row["id"]}: "{row["text"]}" (was "{old_text}"). '
                    'Say "undo" to put the old wording back.'
                )
            return f'Saved memory #{row["id"]}: "{row["text"]}". Say "undo" to forget it.'
        memory_id = int(action.payload["memory_id"])
        row = await self.plugin._repo(ctx.stores).get(action.chat, memory_id)
        if row is None:
            return f"No memory #{memory_id} in this chat."
        if not await self.plugin._delete_memory(action.chat, memory_id, ctx.stores):
            return f"No memory #{memory_id} in this chat."
        return f'Forgot memory #{memory_id}: "{row["text"]}". Say "undo" to save it again.'
