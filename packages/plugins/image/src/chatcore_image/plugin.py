"""Image generation tool, usage command and skill."""

from __future__ import annotations

import logging
import unicodedata

from chatcore.access import AccessPolicy
from chatcore.files import FilePlatform, PhotoPlatform, PhotoReceiptPlatform
from chatcore.images import ImageUnavailable, download_image, is_image
from chatcore.llm.types import ImagePart
from chatcore.platform import ChatPlatform
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.skills import Skill
from chatcore.tools.registry import Tool, ToolContext, ToolResult

from chatcore_image.permissions import may_generate, may_use_reference
from chatcore_image.routing import KEYWORDS, pinned, reference_note
from chatcore_image.service import ImageError, ImageService
from chatcore_image.settings import SIZES, ImageSettings

log = logging.getLogger("chatcore_image")
TOOL = "generate_image"
IMAGE_GENERATION_INSTRUCTIONS = (
    "Generate only when the person explicitly asks for an image. Write one self-contained prompt "
    "that restates the whole picture, resolving references from the conversation. Never put "
    "private individuals' names or personal details in the prompt. Call generate_image once, "
    "then reply in one or two sentences saying what was made. If it fails, say so and offer to "
    "adjust the description."
)
# With the generation text this stays within the 766 characters shipped in v0.17.0, so apps
# that append their own additions keep the same headroom under the 800-character cap.
IMAGE_EDIT_INSTRUCTIONS = (
    "Edit photos sent now or directly replied to; call generate_image with reference. In groups, "
    "anyone may edit a directly replied-to photo. Never use unrelated photos. Never put real "
    "people in sexual, violent, humiliating or deceptive scenes; explain content refusals. "
    "Report actual tool errors; don't invent format or capability errors."
)

GENERATE_SUMMARY = "Create a picture from a description"
EDIT_SUMMARY = (
    "Create a picture from a description, or edit a photo attached or directly replied to"
)
GENERATE_DESCRIPTION = "Create one requested image from a text description and send it to the chat."
EDIT_DESCRIPTION = (
    "Create one requested image and send it to the chat, either from a text description or by "
    "changing a photo the person sent or replied to (pass reference)."
)
REFERENCE_DESCRIPTION = (
    "Pass this whenever the person asks to change, restyle or base a picture on a photo they "
    "sent with this message or replied to; the photo itself is then used. Photos are numbered "
    "from 1: first those in the person's message, then those in the message they replied to, "
    "so a single photo is 1."
)
IMAGE_EDIT_PRIVACY_NOTE = (
    "When photo editing is on, a photo you ask the bot to change is sent to OpenAI with your "
    "request; the bot doesn't store it."
)
_ERRORS = {
    "quota": "Image limit reached for today.",
    "busy": "Image generation is busy; suggest trying again shortly.",
    "refused": "The image provider refused this prompt; offer to change the description.",
    "unavailable": "Image generation is unavailable right now.",
    "invalid_output": "The image provider returned an unusable image.",
}


def _has_control(text: str, *, allow_whitespace: bool) -> bool:
    return any(
        unicodedata.category(char) == "Cc" and not (allow_whitespace and char in "\n\t")
        for char in text
    )


class ImagePlugin(BasePlugin):
    name = "image"
    explainer_package = "chatcore_image"
    help_title = "Image generation"

    def __init__(
        self, access: AccessPolicy, platform: ChatPlatform, *, settings: ImageSettings | None = None
    ):
        self.access, self.platform = access, platform
        self.settings = settings or ImageSettings()
        self.service = ImageService(self.settings)

    @property
    def generation_available(self) -> bool:
        return self.service.available and isinstance(self.platform, (PhotoPlatform, FilePlatform))

    async def aclose(self) -> None:
        await self.service.aclose()

    def tools(self) -> list[Tool]:
        return [self._generate_tool()] if self.generation_available else []

    def tool_gates(self):
        from chatcore_image.gates import build_gates

        return build_gates(self)

    def _generate_tool(self) -> Tool:
        limit = self.settings.IMAGE_MAX_PROMPT_CHARS
        schema = {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Self-contained description of the image to create: subject, "
                    f"style, composition and any text it should show; 1–{limit} characters.",
                },
                "size": {"type": "string", "enum": list(SIZES), "description": "Image shape."},
                "title": {
                    "type": "string",
                    "description": "Short plain title for the image caption; 1–120 characters.",
                },
            },
            "required": ["prompt", "size", "title"],
            "additionalProperties": False,
        }
        if self.settings.IMAGE_EDIT_ENABLED:
            schema["properties"]["reference"] = {
                "type": "integer",
                "minimum": 1,
                "description": REFERENCE_DESCRIPTION,
            }

        async def generate(
            ctx: ToolContext,
            *,
            prompt: str,
            size: str,
            title: str,
            reference: int | None = None,
        ) -> ToolResult:
            if ctx.turn.tool_attempts.get(TOOL):
                return ToolResult.error("An image was already attempted this turn.")
            if reference is not None and (
                not isinstance(reference, int) or isinstance(reference, bool)
            ):
                return ToolResult.error("The photo number must be a whole number.")
            if not isinstance(prompt, str):
                return ToolResult.error(
                    f"Prompt must be 1–{limit} characters without control characters."
                )
            if not isinstance(title, str):
                return ToolResult.error(
                    "Title must be 1–120 characters without newlines or control characters."
                )
            if not isinstance(size, str):
                return ToolResult.error("Size must be square, portrait or landscape.")
            prompt_clean, title_clean = prompt.strip(), title.strip()
            if (
                not prompt_clean
                or len(prompt_clean) > limit
                or _has_control(prompt_clean, allow_whitespace=True)
            ):
                return ToolResult.error(
                    f"Prompt must be 1–{limit} characters without control characters."
                )
            if (
                not title_clean
                or len(title_clean) > 120
                or _has_control(title_clean, allow_whitespace=False)
            ):
                return ToolResult.error(
                    "Title must be 1–120 characters without newlines or control characters."
                )
            if size not in SIZES:
                return ToolResult.error("Size must be square, portrait or landscape.")
            attachment = None
            if reference is not None:
                if not self.settings.IMAGE_EDIT_ENABLED:
                    return ToolResult.error("Editing photos isn't enabled here.")
                try:
                    owned = [
                        (item, owner)
                        for item, owner in zip(
                            ctx.turn.attachments, ctx.turn.attachment_owners, strict=True
                        )
                        if is_image(item)
                    ]
                except ValueError:
                    owned = []
                if not owned:
                    return ToolResult.error(
                        "There's no photo to use. Send it with your request or reply to it."
                    )
                if reference < 1 or reference > len(owned):
                    return ToolResult.error(f"There are {len(owned)} images; pick 1–{len(owned)}.")
                attachment, owner = owned[reference - 1]
                if not may_use_reference(ctx, attachment, owner):
                    return ToolResult.error(
                        "In groups, reply directly to the photo you want to edit."
                    )
            if not self.generation_available or not await may_generate(self.access, ctx):
                return ToolResult.error("Image generation is unavailable here.")
            if ctx.turn.tool_attempts.get(TOOL):
                return ToolResult.error("An image was already attempted this turn.")
            ctx.turn.tool_attempts[TOOL] = 1
            caption = "🖼 " + title_clean
            try:
                part: ImagePart | None = None
                if attachment is not None:
                    try:
                        part = await download_image(
                            ctx.platform, attachment, chat=ctx.chat, cache=ctx.turn.downloads
                        )
                    except ImageUnavailable as exc:
                        message = {
                            "too_large": "That photo is over 5 MB.",
                            "unsupported_format": "I can only use JPEG, PNG or WebP photos.",
                            "download_failed": (
                                "I couldn't download that photo. Please send it again."
                            ),
                        }[exc.reason]
                        return ToolResult.error(message)
                data = await self.service.generate(
                    prompt_clean, size, user_key=ctx.user.ref.key, reference=part
                )
                if not await may_generate(self.access, ctx):
                    return ToolResult.error("Image delivery is no longer allowed.")
                if self.settings.IMAGE_EDIT_ENABLED and isinstance(
                    ctx.platform, PhotoReceiptPlatform
                ):
                    receipt = await ctx.platform.send_photo_with_receipt(
                        ctx.chat, "image.png", data, caption=caption, thread_id=ctx.thread_id
                    )
                    try:
                        if (
                            receipt.message.chat != ctx.chat
                            or receipt.message.thread_id != ctx.thread_id
                            or receipt.attachment is None
                        ):
                            raise ValueError("Invalid photo receipt")
                        await ctx.stores.repo.log_bot_message(
                            ctx.chat,
                            receipt.message.message_id,
                            caption,
                            thread_id=ctx.thread_id,
                            attachments=(receipt.attachment,),
                            attachment_requester_key=ctx.user.ref.key,
                        )
                    except Exception as exc:
                        log.warning("image reference recording failed error=%s", type(exc).__name__)
                        return ToolResult(
                            "Image sent successfully, but its reference could not be saved. "
                            "For another edit, ask the person to upload the image again."
                        )
                elif isinstance(ctx.platform, PhotoPlatform):
                    await ctx.platform.send_photo(
                        ctx.chat, "image.png", data, caption=caption, thread_id=ctx.thread_id
                    )
                else:
                    await ctx.platform.send_file(
                        ctx.chat, "image.png", data, caption=caption, thread_id=ctx.thread_id
                    )
                return ToolResult(
                    "Image sent before the text response. Now reply in one or two sentences "
                    "saying what was made."
                )
            except ImageError as exc:
                return ToolResult.error(_ERRORS.get(exc.code, _ERRORS["unavailable"]))
            except Exception as exc:
                log.warning("image delivery failed error=%s", type(exc).__name__)
                return ToolResult.error("Could not deliver the image.")

        return Tool(
            TOOL,
            EDIT_DESCRIPTION if self.settings.IMAGE_EDIT_ENABLED else GENERATE_DESCRIPTION,
            schema,
            generate,
            writes=False,
        )

    def skills(self) -> list[Skill]:
        if not self.generation_available:
            return []
        edit_enabled = self.settings.IMAGE_EDIT_ENABLED

        async def pin(ctx: ToolContext) -> bool:
            return pinned(ctx, edit_enabled=edit_enabled)

        async def context(ctx: ToolContext) -> str | None:
            return reference_note(ctx) if edit_enabled else None

        return [
            Skill(
                "image_generation",
                EDIT_SUMMARY if edit_enabled else GENERATE_SUMMARY,
                (
                    "draw a cat wearing a space helmet",
                    "make a picture of a sunset over the Singapore skyline",
                    "make this photo a watercolour",
                    "把这张照片变成卡通",
                    "generate an image for my birthday invite",
                    "can you illustrate this story",
                    "画一只在海边的狗",
                    "create a logo idea for a bubble tea shop",
                ),
                (TOOL,),
                keywords=KEYWORDS,
                commands=("image",),
                max_steps=4,
                instructions=(
                    IMAGE_GENERATION_INSTRUCTIONS + " " + IMAGE_EDIT_INSTRUCTIONS
                    if self.settings.IMAGE_EDIT_ENABLED
                    else IMAGE_GENERATION_INSTRUCTIONS
                ),
                context=context,
                pin=pin,
            )
        ]

    def commands(self) -> list[Command]:
        async def image(ctx: CommandContext, args: str) -> str:
            if args.strip().casefold() != "usage":
                return f"Use {ctx.command_form('image')} usage to see image usage."
            if not self.service.available:
                return "Image generation is not configured."
            report = await self.service.report()
            return (
                f"Images today (UTC): {report['total']:,} of {report['daily_limit']:,}.\n"
                f"Model: {report['model']}, quality: {report['quality']}."
            )

        return [Command("image", "Show image generation usage (admins)", image, admin_only=True)]
