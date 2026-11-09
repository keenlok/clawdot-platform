"""Read-aloud narration tool, admin usage command and skill."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from chatcore.access import AccessPolicy
from chatcore.files import VoicePlatform
from chatcore.platform import ChatPlatform
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.skills import Skill
from chatcore.tools.registry import Tool, ToolContext, ToolResult
from chatcore.tts import valid_speech_text

from chatcore_voice.permissions import may_narrate
from chatcore_voice.service import NarrationError, NarrationService
from chatcore_voice.settings import VoiceSettings

log = logging.getLogger("chatcore_voice")

TOOL = "narrate_text"
READ_ALOUD_INSTRUCTIONS = (
    "Narrate only when the person explicitly asks to hear something: read aloud, voice note, "
    "audio version, narrate. First write the passage as plain spoken prose: no markdown, URLs, "
    "tables or emoji; say sources and numbers in words. For news, gather a few current sources "
    "with the web tools before writing. Keep it within the tool's length limit by summarising, "
    "never by cutting off mid-sentence. Call narrate_text once with exactly that passage, its "
    "language and a short title. Then reply with the same passage as text, adding any links. "
    "If narration fails, still give the text and say the voice note couldn't be sent."
)

_ERRORS = {
    "quota": "Narration limit reached for now; reply in text only.",
    "provider_quota": "The voice provider allowance is nearly used up; reply in text only.",
    "busy": "Narration is busy; reply in text only and suggest trying again shortly.",
    "invalid_text": "Invalid narration text: plain text within the length limit is required.",
    "invalid_language": "That language has no narration voice here.",
}


class VoicePlugin(BasePlugin):
    name = "voice"
    explainer_package = "chatcore_voice"
    help_title = "Voice narration"

    def __init__(
        self,
        access: AccessPolicy,
        platform: ChatPlatform,
        *,
        settings: VoiceSettings | None = None,
    ) -> None:
        self.access, self.platform = access, platform
        self.settings = settings or VoiceSettings()
        self.narration = NarrationService(self.settings)

    @property
    def narration_available(self) -> bool:
        return self.narration.available and isinstance(self.platform, VoicePlatform)

    async def aclose(self) -> None:
        await self.narration.aclose()

    def tools(self) -> list[Tool]:
        return [self._narrate_tool()] if self.narration_available else []

    def tool_gates(self):
        from chatcore_voice.gates import build_gates

        return build_gates(self)

    def _narrate_tool(self) -> Tool:
        limit = self.settings.VOICE_TTS_MAX_CHARS_PER_REQUEST
        languages = list(self.narration.languages)
        schema = {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": f"Exactly the passage to read aloud; plain spoken prose, no "
                    f"markdown or links; 1–{limit} characters.",
                },
                "language": {
                    "type": "string",
                    "enum": languages,
                    "description": "Language the passage is written in.",
                },
                "title": {
                    "type": "string",
                    "description": "Short plain title for the voice note caption; 1–120 "
                    "characters.",
                },
            },
            "required": ["text", "language", "title"],
            "additionalProperties": False,
        }

        async def narrate(ctx: ToolContext, *, text: str, language: str, title: str) -> ToolResult:
            if ctx.turn.tool_attempts.get(TOOL, 0):
                return ToolResult.error("A narration was already attempted this turn.")
            if (
                not valid_speech_text(text, max_chars=limit)
                or not valid_speech_text(title, max_chars=120)
                or "\n" in title
                or "\r" in title
                or language not in languages
            ):
                return ToolResult.error(_ERRORS["invalid_text"])
            if not self.narration_available or not await may_narrate(self.access, ctx):
                return ToolResult.error("Narration is unavailable here.")
            if ctx.turn.tool_attempts.get(TOOL, 0):
                return ToolResult.error("A narration was already attempted this turn.")
            ctx.turn.tool_attempts[TOOL] = 1
            caption = "🔊 " + title.strip()
            try:
                audio = await self.narration.narrate(text, language, user_key=ctx.user.ref.key)
                if not await may_narrate(self.access, ctx):
                    return ToolResult.error("Narration delivery is no longer allowed.")
                await ctx.platform.send_voice(
                    ctx.chat, "narration.mp3", audio, caption=caption, thread_id=ctx.thread_id
                )
                return ToolResult(
                    "Voice narration sent before the text response. Now reply with the same "
                    "passage as text."
                )
            except NarrationError as exc:
                return ToolResult.error(
                    _ERRORS.get(exc.code, "Could not generate the narration; reply in text only.")
                )
            except Exception as exc:
                log.warning("voice narration delivery failed error=%s", type(exc).__name__)
                return ToolResult.error("Could not deliver the narration; reply in text only.")

        return Tool(
            TOOL,
            "Read one requested passage aloud as a voice message.",
            schema,
            narrate,
            writes=False,
        )

    def skills(self) -> list[Skill]:
        if not self.narration_available:
            return []
        return [
            Skill(
                "read_aloud",
                "Read a passage aloud as a voice message, such as a news summary",
                (
                    "read that out loud",
                    "send me a voice note of the summary",
                    "narrate today's news on AI",
                    "can you give me an audio version",
                    "read this aloud for me",
                    "把这段总结念出来",
                    "把今天的新闻念给我听",
                    "can send voice note of the news or not",
                ),
                (TOOL,),
                keywords=(
                    "read aloud",
                    "read it out",
                    "read that out",
                    "out loud",
                    "voice note",
                    "voice message",
                    "audio version",
                    "narrate",
                    "narration",
                    "念给我听",
                    "读出来",
                ),
                commands=("voice",),
                max_steps=4,
                instructions=READ_ALOUD_INSTRUCTIONS,
            )
        ]

    def commands(self) -> list[Command]:
        async def voice(ctx: CommandContext, args: str) -> str:
            if args.strip().casefold() != "usage":
                return f"Use {ctx.command_form('voice')} usage to see narration usage."
            if not self.narration.available:
                return "Voice narration is not configured."
            report = await self.narration.report()
            lines = [
                f"Narration today (UTC): {report.day_chars:,} of {report.day_limit:,} characters."
            ]
            usage = report.provider
            if usage is None:
                lines.append(f"Provider usage unavailable ({report.provider_error}).")
            else:
                lines.append(
                    f"Provider billing period: {usage.character_count:,} of "
                    f"{usage.character_limit:,} used; {max(usage.remaining, 0):,} left."
                )
                if usage.next_reset_unix is not None:
                    try:
                        reset = datetime.fromtimestamp(usage.next_reset_unix, UTC)
                    except (OverflowError, OSError, ValueError):
                        reset = None
                    if reset is not None:
                        lines.append(f"Resets {reset:%Y-%m-%d %H:%M} UTC.")
                lines.append(
                    "Overage billing is possible."
                    if usage.overage_possible
                    else "Overage billing is off."
                )
            return "\n".join(lines)

        return [
            Command("voice", "Show voice narration usage (admins)", voice, admin_only=True),
        ]
