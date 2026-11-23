"""Read-aloud narration tool, admin usage command and skill."""

from __future__ import annotations

import logging
import shlex
from collections import OrderedDict
from datetime import UTC, datetime

from chatcore.access import AccessPolicy
from chatcore.events import AttachmentKind, IncomingMessage
from chatcore.files import VoicePlatform
from chatcore.platform import ChatPlatform
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.skills import Skill
from chatcore.tools.registry import Tool, ToolContext, ToolResult
from chatcore.tts import valid_speech_text

from chatcore_voice.input import TranscriptionService
from chatcore_voice.permissions import may_change_voice_settings, may_narrate
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
    "language and a short title. For Cantonese, write colloquial Cantonese in traditional "
    "characters, not Mandarin. Then reply with the same passage as text, adding any links. "
    "If narration fails, give the text and one short line explaining the tool's reason "
    "for no audio."
)

_ERRORS = {
    "quota": "Narration limit reached for now; reply in text only.",
    "provider_quota": "The voice provider allowance is nearly used up; reply in text only.",
    "busy": "Narration is busy; reply in text only and suggest trying again shortly.",
    "invalid_text": "Invalid narration text: plain text within the length limit is required.",
    "invalid_language": "That language has no narration voice here.",
    "long": "That's a long voice note. I can take notes up to 2:00. Can you split it or type it?",
    "unheard": "I couldn't make that out. Try again closer to the mic, or type it.",
    "stt_quota": "I've hit today's voice-note limit. Can you type it?",
    "provider": "Voice notes aren't working right now. Can you type it?",
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
        self.transcription = TranscriptionService(self.settings)
        self._voice_turn_ids: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.migrations_package = "chatcore_voice.migrations"

    @property
    def narration_available(self) -> bool:
        return self.narration.available and isinstance(self.platform, VoicePlatform)

    async def aclose(self) -> None:
        await self.narration.aclose()
        await self.transcription.aclose()

    async def _chat_settings(self, stores, chat):
        if stores.db is None:
            return {
                "transcribe": True,
                "wake_word_enabled": False,
                "wake_words": [],
                "voice_reply": True,
                "show_transcript": None,
            }
        row = await stores.db.fetchrow(
            "SELECT * FROM voice_chat_settings WHERE chat_key=$1", chat.key
        )
        return (
            dict(row)
            if row
            else {
                "transcribe": True,
                "wake_word_enabled": False,
                "wake_words": [],
                "voice_reply": True,
                "show_transcript": None,
            }
        )

    async def voice_reply_enabled(self, stores, chat) -> bool:
        return bool(
            self.narration_available and (await self._chat_settings(stores, chat))["voice_reply"]
        )

    async def transcript_prefix(self, stores, chat, *, is_dm, transcript):
        setting = (await self._chat_settings(stores, chat))["show_transcript"]
        enabled = (not is_dm) if setting is None else bool(setting)
        if not enabled:
            return ""
        spoken = " ".join(transcript.split())[:400].replace("`", "'")
        return f"> 🎙 heard: `{spoken}`\n\n"

    async def transform_incoming(self, message: IncomingMessage, app) -> IncomingMessage | None:
        if not self.transcription.available:
            return message
        source = "self"
        voice = next(
            (
                a
                for a in message.attachments
                if a.kind in {AttachmentKind.VOICE, AttachmentKind.AUDIO}
            ),
            None,
        )
        if (
            voice is not None
            and voice.kind is AttachmentKind.AUDIO
            and not (message.mentions_bot or message.reply_to_bot)
        ):
            return message
        if voice is None and message.mentions_bot and message.reply_to is not None:
            replied = await app.stores.repo.get_message(message.chat, message.reply_to.message_id)
            voice = next(
                (
                    a
                    for a in (replied.attachments if replied else ())
                    if a.kind in {AttachmentKind.VOICE, AttachmentKind.AUDIO}
                ),
                None,
            )
            if voice is not None:
                source = "replied"
        if voice is None:
            return message
        if message.forwarded and source == "self":
            return message
        settings = await self._chat_settings(app.stores, message.chat)
        if not settings["transcribe"]:
            return message
        wake_words = tuple(settings["wake_words"] or ()) if settings["wake_word_enabled"] else ()
        addressed = message.is_dm or message.reply_to_bot or message.mentions_bot
        if not addressed and not wake_words:
            return message
        if addressed:
            await self._react_listening(message)
        result, error = await self.transcription.transcribe(
            message,
            app,
            voice,
            wake_words=wake_words if source == "self" and not addressed else (),
            source=source,
        )
        if error in {"wake_miss", "wake_long"}:
            return message
        if error and not addressed:
            # Wake mode: a note nobody addressed to the bot never gets a refusal in the group.
            return message
        if error:
            reason = _ERRORS.get(error, _ERRORS["provider"])
            if error == "long":
                seconds = voice.duration_s or round((voice.size_bytes or 0) / 16_000)
                length = self.settings.VOICE_STT_MAX_SECONDS_PER_NOTE
                reason = (
                    f"That's {seconds // 60}:{seconds % 60:02d}. I can take voice notes up to "
                    f"{length // 60}:{length % 60:02d}. Can you split it or type it?"
                )
            if error != "provider" or self.settings.VOICE_STT_ENABLED:
                await app.send(
                    message.chat,
                    reason,
                    reply_to=message.ref,
                    thread_id=message.thread_id,
                )
                return None
            return message
        if result.voice is not None and not addressed:
            # Wake mode: react only once the wake word matched, never to unaddressed notes.
            await self._react_listening(message)
        if result.voice is not None:
            key = (message.chat.key, message.message_id)
            self._voice_turn_ids[key] = None
            self._voice_turn_ids.move_to_end(key)
            while len(self._voice_turn_ids) > 256:
                self._voice_turn_ids.popitem(last=False)
        return result

    async def _react_listening(self, message: IncomingMessage) -> None:
        try:
            if hasattr(self.platform, "react"):
                await self.platform.react(message.ref, "👂")
        except Exception as exc:
            log.info("voice reaction failed error=%s", type(exc).__name__)

    def tools(self) -> list[Tool]:
        tools = [self._narrate_tool()] if self.narration_available else []
        if self.narration_available or self.transcription.available:
            tools.append(self._settings_tool())
        return tools

    def tool_gates(self):
        from chatcore_voice.gates import build_gates

        return build_gates(self)

    async def _may_change_settings(
        self, *, stores, chat, user, is_dm: bool, is_admin: bool, platform
    ) -> bool:
        return await may_change_voice_settings(
            self.access,
            chat=chat,
            user=user,
            is_dm=is_dm,
            platform=platform,
            cache=stores.cache,
            is_admin=is_admin,
        )

    async def _update_settings(
        self, stores, chat, user, platform, *, is_dm, is_admin, setting, value
    ):
        if not await self._may_change_settings(
            stores=stores, chat=chat, user=user, is_dm=is_dm, is_admin=is_admin, platform=platform
        ):
            return "Only a group admin can change voice settings."
        current = await self._chat_settings(stores, chat)
        column = {
            "reply": "voice_reply",
            "transcribe": "transcribe",
            "transcript": "show_transcript",
        }.get(setting)
        if setting == "wakeword":
            if value.casefold() == "off":
                values = (False, [])
            else:
                phrases = [p.strip() for p in value.split("|")]
                if not 1 <= len(phrases) <= 5 or any(
                    len(p) > 40 or not 1 <= len(p.split()) <= 4 for p in phrases
                ):
                    return "Use up to five wake phrases, each one to four words and 40 characters."
                values = (True, phrases)
            if stores.db is None:
                return "Voice settings storage is unavailable."
            if values[0] and not current["wake_word_enabled"]:
                await platform.send(
                    chat,
                    "Voice notes in this group are now sent to ElevenLabs for transcription. "
                    "Start with the wake word to address the bot.",
                )
            await stores.db.execute(
                "INSERT INTO voice_chat_settings(chat_key,wake_word_enabled,wake_words,updated_by) "
                "VALUES($1,$2,$3,$4) ON CONFLICT(chat_key) DO UPDATE SET "
                "wake_word_enabled=$2,wake_words=$3,updated_by=$4,updated_at=now()",
                chat.key,
                values[0],
                values[1],
                user.ref.key,
            )
            return "Wake-word setting updated."
        if column is None or value.casefold() not in {"on", "off"}:
            return "Choose reply, transcribe, transcript or wakeword, with on or off."
        if stores.db is None:
            return "Voice settings storage is unavailable."
        val = value.casefold() == "on"
        await stores.db.execute(
            f"INSERT INTO voice_chat_settings(chat_key,{column},updated_by) VALUES($1,$2,$3) "
            f"ON CONFLICT(chat_key) DO UPDATE SET {column}=$2,updated_by=$3,updated_at=now()",
            chat.key,
            val,
            user.ref.key,
        )
        return f"Voice {setting} set to {value.casefold()}."

    def _settings_tool(self) -> Tool:
        schema = {
            "type": "object",
            "properties": {
                "setting": {
                    "type": "string",
                    "enum": ["reply", "transcribe", "transcript", "wakeword"],
                },
                "value": {
                    "type": "string",
                    "description": "on/off, or wake phrases separated by |",
                },
            },
            "required": ["setting", "value"],
            "additionalProperties": False,
        }

        async def update(ctx, *, setting: str, value: str) -> ToolResult:
            result = await self._update_settings(
                ctx.stores,
                ctx.chat,
                ctx.user,
                ctx.platform,
                is_dm=ctx.is_dm,
                is_admin=ctx.is_admin,
                setting=setting,
                value=value,
            )
            if (
                result.startswith("Only ")
                or result.endswith("unavailable.")
                or result.startswith("Use up")
            ):
                return ToolResult.error(result)
            return ToolResult(result)

        return Tool(
            "voice_settings",
            "Change per-chat voice input and reply settings.",
            schema,
            update,
            writes=True,
        )

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
                activity = getattr(ctx.platform, "activity", None)
                if callable(activity):
                    try:
                        await activity(ctx.chat, "record_voice", ctx.thread_id)
                    except Exception as exc:
                        log.info("voice activity failed error=%s", type(exc).__name__)
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
        async def pin(ctx):
            key = (ctx.chat.key, ctx.source_message_id or "")
            voice_turn = key in self._voice_turn_ids
            self._voice_turn_ids.pop(key, None)
            return bool(voice_turn and await self.voice_reply_enabled(ctx.stores, ctx.chat))

        skills = (
            [
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
                        "幫我將今日嘅新聞讀出嚟",
                    ),
                    (TOOL,),
                    final_tools=(TOOL,),
                    pin=pin,
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
                        "讀出嚟",
                        "念出嚟",
                        "唸出嚟",
                    ),
                    commands=("voice",),
                    max_steps=4,
                    instructions=READ_ALOUD_INSTRUCTIONS,
                )
            ]
            if self.narration_available
            else []
        )
        if self.narration_available or self.transcription.available:
            skills.append(
                Skill(
                    "voice_controls",
                    "Change voice transcription, transcript display and voice reply settings",
                    (
                        "turn voice replies off",
                        "stop transcribing voice notes",
                        "set the group wake word",
                        "show the voice settings",
                    ),
                    ("voice_settings",),
                    keywords=("voice settings", "voice replies", "wake word", "transcribe voice"),
                    commands=("voice",),
                    instructions=(
                        "Use voice_settings to change only the requested per-chat voice option. "
                        "Wake words are up to five phrases of one to four words each."
                    ),
                )
            )
        return skills

    def commands(self) -> list[Command]:
        async def voice(ctx: CommandContext, args: str) -> str:
            if args.strip().casefold() == "usage":
                if not ctx.is_admin:
                    return "Voice usage is available to admins only."
                if not self.narration.available:
                    return "Voice narration is not configured."
                report = await self.narration.report()
                lines = [
                    f"Narration today (UTC): {report.day_chars:,} of "
                    f"{report.day_limit:,} characters."
                ]
                usage = report.provider
                if usage is None:
                    lines.append(f"Provider usage unavailable ({report.provider_error}).")
                else:
                    lines.append(
                        f"Provider billing period: {usage.character_count:,} of "
                        f"{usage.character_limit:,} used; {max(usage.remaining, 0):,} left."
                    )
                    lines.append(
                        "Overage billing is possible."
                        if usage.overage_possible
                        else "Overage billing is off."
                    )
                    if usage.next_reset_unix is not None:
                        try:
                            reset = datetime.fromtimestamp(usage.next_reset_unix, UTC)
                        except (OverflowError, OSError, ValueError):
                            reset = None
                        if reset is not None:
                            lines.append(f"Resets {reset:%Y-%m-%d} UTC.")
                return "\n".join(lines)
            try:
                parts = shlex.split(args)
            except ValueError:
                return "Unbalanced quotes in wake phrases."
            if len(parts) >= 2:
                setting = parts[0].casefold()
                if setting == "wakeword":
                    value = (
                        "off"
                        if len(parts) == 2 and parts[1].casefold() == "off"
                        else "|".join(parts[1:])
                    )
                else:
                    value = " ".join(parts[1:])
                return await self._update_settings(
                    ctx.stores,
                    ctx.chat,
                    ctx.user,
                    ctx.platform,
                    is_dm=ctx.is_dm,
                    is_admin=ctx.is_admin,
                    setting="wakeword" if setting == "wakeword" else setting,
                    value=value,
                )
            settings = await self._chat_settings(ctx.stores, ctx.chat)
            wake_words = ", ".join(settings["wake_words"] or ()) or "none configured"
            display = settings["show_transcript"]
            display_text = (
                ("on" if display else "off")
                if display is not None
                else ("on (default)" if not ctx.is_dm else "off (default)")
            )
            form = ctx.command_form("voice")
            return (
                f"Voice settings (text commands):\n"
                f"Voice replies: "
                f"{'on' if settings['voice_reply'] else 'off'}; "
                f"transcription: {'on' if settings['transcribe'] else 'off'}; "
                f"wake word: {'on' if settings['wake_word_enabled'] else 'off'} "
                f"({wake_words}); transcript display: {display_text}.\n"
                f"Commands: {form} reply on|off; {form} transcribe on|off; "
                f'{form} transcript on|off; {form} wakeword "phrase" "alternate" | off; '
                f"{form} usage."
            )

        return [
            Command("voice", "Show or change voice settings", voice),
        ]
