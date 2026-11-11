"""Structured card tool and skill."""

from __future__ import annotations

import logging
import unicodedata

from chatcore.access import AccessPolicy
from chatcore.cards import (
    CardError,
    can_deliver_cards,
    card_json_schema,
    send_card_pages,
    validate_card,
)
from chatcore.platform import ChatPlatform
from chatcore.plugin import BasePlugin
from chatcore.skills import Skill
from chatcore.tools.registry import Tool, ToolContext, ToolResult

from chatcore_cards.permissions import may_send_card
from chatcore_cards.service import CardService
from chatcore_cards.settings import CardsSettings

log = logging.getLogger("chatcore_cards")
TOOL = "send_card"
VISUAL_SUMMARY_INSTRUCTIONS = (
    "Use send_card when the person asks for a card, picture or table, or when a reply would need a "
    "table of 3+ rows or several headline numbers. Lead with the answer: stats or a callout first, "
    "then detail. One idea per block; bullets over paragraphs; tables only for 3+ rows. "
    "Put numbers "
    "exactly as the tools returned them, units in column headers. Add sources for web facts. Never "
    "put instructions, HTML or markdown in card text. Call send_card once, then reply in 1–3 short "
    "lines with the key result and any links. If it fails, answer in normal text."
)


class CardsPlugin(BasePlugin):
    name = "cards"
    explainer_package = "chatcore_cards"
    help_title = "Summary cards"

    def __init__(
        self, access: AccessPolicy, platform: ChatPlatform, *, settings=None, service=None
    ):
        self.access, self.platform = access, platform
        self.settings = settings or CardsSettings()
        self.service = service or CardService(self.settings)

    @property
    def cards_available(self) -> bool:
        return self.service.available and can_deliver_cards(self.platform)

    async def aclose(self):
        await self.service.aclose()

    def tool_gates(self):
        from chatcore_cards.gates import build_gates

        return build_gates(self)

    def tools(self):
        return [self._send_tool()] if self.cards_available else []

    def _send_tool(self):
        schema = card_json_schema()
        schema["properties"]["caption"] = {
            "type": "string",
            "description": "Plain text photo caption, 1–200 characters.",
            "minLength": 1,
            "maxLength": 200,
        }
        schema["required"] = [*schema["required"], "caption"]

        async def send(ctx: ToolContext, *, caption: str, **data) -> ToolResult:
            if ctx.turn.tool_attempts.get(TOOL):
                return ToolResult.error("A card was already attempted this turn.")
            if (
                not isinstance(caption, str)
                or not caption.strip()
                or len(caption.strip()) > 200
                or any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in caption)
            ):
                return ToolResult.error(
                    "Invalid card: caption: must be 1–200 plain text characters."
                )
            try:
                card = validate_card(data, max_text_chars=self.settings.CARDS_MAX_TEXT_CHARS)
            except CardError as exc:
                return ToolResult.error(f"Invalid card: {exc.detail}")
            if not self.cards_available or not await may_send_card(self.access, ctx):
                return ToolResult.error("Cards are unavailable here.")
            ctx.turn.tool_attempts[TOOL] = 1
            try:
                pages = await self.service.render(card, user_key=ctx.user.ref.key)
                if not await may_send_card(self.access, ctx):
                    return ToolResult.error("Card delivery is no longer allowed.")
                await send_card_pages(
                    ctx.platform,
                    ctx.chat,
                    pages,
                    caption="📋 " + caption.strip(),
                    thread_id=ctx.thread_id,
                )
                return ToolResult(
                    "Card sent above the text reply. Now reply in 1–3 short lines with the key "
                    "result and any links; don't repeat the whole card."
                )
            except CardError as exc:
                messages = {
                    "quota": "Card limit reached for now; answer in text.",
                    "busy": "The card renderer is busy; answer in text.",
                    "too_large": "That card is too long; shorten it or answer in text.",
                    "timeout": "Couldn't make the card; answer in text.",
                    "unavailable": "Couldn't make the card; answer in text.",
                }
                return ToolResult.error(
                    messages.get(exc.code, "Couldn't deliver the card; answer in text.")
                )
            except Exception as exc:
                log.warning("card delivery failed error=%s", type(exc).__name__)
                return ToolResult.error("Couldn't deliver the card; answer in text.")

        return Tool(
            TOOL,
            "Send a structured answer as one clean summary card image; the text reply follows.",
            schema,
            send,
            writes=False,
        )

    def skills(self):
        if not self.cards_available:
            return []
        return [
            Skill(
                "visual_summary",
                "Show a structured answer as a clean summary card",
                (
                    "make it a summary card",
                    "show this as a table",
                    "send a nice summary of the research",
                    "整理成一张图",
                    "可以做成图片吗",
                    "can show as a picture or not",
                    "make a quick card with the key numbers",
                    "put the results in a card",
                ),
                (TOOL,),
                keywords=(
                    "summary card",
                    "as a card",
                    "as an image",
                    "as a table",
                    "做成图",
                    "整理成一张图",
                    "卡片总结",
                ),
                max_steps=6,
                max_output_tokens=12288,
                instructions=VISUAL_SUMMARY_INSTRUCTIONS,
            )
        ]
