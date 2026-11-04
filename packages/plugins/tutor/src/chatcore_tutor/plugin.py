"""Private tutor profile command, read tool and language-practice skill."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from chatcore.command_prompts import CommandReply
from chatcore.events import AttachmentKind, ChatRef, IncomingMessage, UserRef
from chatcore.files import VoicePlatform
from chatcore.pending import PendingAction
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.skills import Skill
from chatcore.store.base import Stores
from chatcore.tools.registry import Tool, ToolContext, ToolResult

from chatcore_tutor.audio import valid_tutor_text
from chatcore_tutor.languages import LANGUAGES, LEVELS, Language, lookup_language, parse_level
from chatcore_tutor.models import TutorProfile, TutorTopic
from chatcore_tutor.permissions import is_group_manager, may_pronounce
from chatcore_tutor.repository import MemoryTutorRepo, TutorRepo
from chatcore_tutor.settings import TutorSettings
from chatcore_tutor.speech import SpeechError, SpeechService

log = logging.getLogger(__name__)

LANGUAGE_TUTOR_INSTRUCTIONS = (
    "Apply only when the person practises a language, asks about one, or requests a translation. "
    "A passing language mention (food, class, person, place) is not practice; ignore this section "
    "and handle the request normally. Use the profile's explanation language; if absent, the "
    "person's usual language. It overrides message language, even if target. Each target-language "
    "line gets bracketed romanisation for non-Latin scripts, then translation in explanation "
    "language. Corrections: their words → fix + brief reason; max three, don't rewrite all. Match "
    "CEFR: A1–A2 short everyday sentences; B1–B2 natural phrases/common idioms; C1–C2 "
    "nuance/register. In practice, end with one short prompt. For plain translation, give "
    "translation + romanisation, no lesson. Otherwise help normally in explanation language."
)
PRONUNCIATION_INSTRUCTIONS = (
    "For an explicit request to hear Cantonese, Mandarin Chinese or Japanese, call "
    "pronounce_tutor_text once with the phrase and Jyutping, tone-marked Pinyin or kana. Select "
    "Cantonese or Mandarin explicitly. Use slow only on request; clip comes before text."
)
_ACTIVE_LANGUAGE_TUTOR_INSTRUCTIONS = (
    "Use for practice, questions and translation; ignore passing mentions. Explain in the profile "
    "or usual language, even if target; prefer it to message language. Give each non-Latin "
    "target line bracketed romanisation then translation. Correct as: their words → fix + brief "
    "reason (max three; never rewrite all). Match CEFR: A1–A2 short everyday sentences; B1–B2 "
    "natural phrases/idioms; C1–C2 nuance/register. End practice with one short prompt. Plain "
    "translation gets translation + romanisation, no lesson. Otherwise use the explanation "
    "language."
)

_EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
_ALIAS_TO_LANGUAGE = {
    alias.casefold(): language
    for language in LANGUAGES
    for alias in (language.name, *language.aliases)
}
_MULTIWORD_ALIASES = sorted(
    (alias for alias in _ALIAS_TO_LANGUAGE if " " in alias), key=len, reverse=True
)
_MULTIWORD_PATTERN = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(alias) for alias in _MULTIWORD_ALIASES) + r")(?!\w)"
)
_LANGUAGE_LIST = ", ".join(language.name for language in LANGUAGES)
_USAGE = "Use /tutor <language> <level> [<explanation languages>]. Supported languages: " + (
    _LANGUAGE_LIST + ". Levels: " + ", ".join(LEVELS) + "."
)
_USAGE_HINT = "Use /tutor <language> <level> [<explanation languages>]; /tutor off pauses."


def _valid_pronunciation_request(
    text: object, language: object, pronunciation: object, pace: object
) -> bool:
    if (
        not valid_tutor_text(text)
        or not valid_tutor_text(pronunciation, max_chars=240)
        or not isinstance(language, str)
        or language not in {"Cantonese", "Mandarin Chinese", "Japanese"}
        or not isinstance(pace, str)
        or pace not in {"normal", "slow"}
    ):
        return False
    return True


@dataclass(frozen=True, slots=True)
class TutorArgs:
    target: str
    level: str
    explain: tuple[str, ...]


def parse_tutor_args(args: str) -> TutorArgs:
    if len(args) > 200:
        raise ValueError('Tutor settings are too long (offending input: "…")')
    source = args.casefold().strip()
    alias_tokens: dict[str, Language] = {}

    def replace_multiword(match: re.Match[str]) -> str:
        token = f"language{len(alias_tokens)}"
        alias_tokens[token] = _ALIAS_TO_LANGUAGE[match.group(0).casefold()]
        return token

    source = _MULTIWORD_PATTERN.sub(replace_multiword, source)
    source = re.sub(r"\s+and\s+", " ", source)
    source = re.sub(r"[,/&+]", " ", source)
    levels = []
    languages = []
    for token in source.split():
        level = parse_level(token)
        if level is not None:
            levels.append(level)
            continue
        if re.fullmatch(r"[abc][0-9]", token):
            raise ValueError(f'Give one level: {", ".join(LEVELS)} (got "{token.upper()}").')
        language = alias_tokens.get(token) or lookup_language(token)
        if language is None:
            raise ValueError(f'Unknown language "{token}". Supported: {_LANGUAGE_LIST}.')
        languages.append(language)
    if len(languages) > 3:
        raise ValueError("At most two explanation languages.")
    if len(levels) != 1:
        offending = ", ".join(levels) if levels else "none"
        raise ValueError(f'Give one level: {", ".join(LEVELS)} (got "{offending}").')
    if not languages:
        raise ValueError('Give one target language (got "none").')
    target = languages[0]
    explanations = tuple(language.name for language in languages[1:])
    if target.name in explanations:
        raise ValueError(
            f"Explanation languages must differ from the language you're learning ({target.name})."
        )
    if len(set(explanations)) != len(explanations):
        repeated = next(name for name in explanations if explanations.count(name) > 1)
        raise ValueError(f'Explanation languages must differ from each other ("{repeated}").')
    return TutorArgs(target.name, levels[0], explanations)


def _explanations_text(explain: tuple[str, ...] | str | None) -> str:
    if not explain:
        return ""
    languages = tuple(explain.split(",")) if isinstance(explain, str) else explain
    return " and ".join(languages)


def _context_explanation(explain: tuple[str, ...] | str | None, fallback: str) -> str:
    if not explain:
        return fallback
    languages = tuple(explain.split(",")) if isinstance(explain, str) else explain
    if len(languages) == 1:
        return f"explain in {languages[0]}"
    return f"explain in {languages[0]}, adding {languages[1]} where it helps"


def _profile_line(profile: TutorProfile) -> str:
    explanations = _explanations_text(profile.explain)
    return (
        f"{profile.target}, level {profile.level}, explanations in {explanations}, "
        f"{'on' if profile.active else 'off'}"
    )


def _topic_status_line(topic: TutorTopic | None) -> str:
    if topic is None:
        return "Tutor topic: off"
    if topic.target is None:
        status = "Tutor topic: on, no default language"
    else:
        status = f"Tutor topic: on, default {topic.target} {topic.level}"
        if topic.explain:
            status += f", explanations in {_explanations_text(topic.explain)}"
    return status + (
        "; replies to every message" if topic.auto_reply else "; replies only when tagged"
    )


def _group_reply_usage(ctx: CommandContext) -> str:
    command = ctx.command_form("tutor")
    return (
        f"Use {command} here <language> <level> [<explanation languages>], {command} here on, "
        f"{command} here off, {command} here auto or {command} here quiet. You can also reply "
        'with "japanese A2 english", "auto", "quiet", "on" or "off".'
    )


def _group_reply_prompt(ctx: CommandContext) -> CommandReply:
    return CommandReply(
        _group_reply_usage(ctx),
        awaits_reply=True,
        placeholder="e.g. japanese A2 english",
    )


class TutorPlugin(BasePlugin):
    name = "tutor"
    explainer_package = "chatcore_tutor"
    help_title = "Language tutor"
    migrations_package = "chatcore_tutor.migrations"

    def __init__(self, access, platform, *, settings: TutorSettings | None = None) -> None:
        self.access, self.platform = access, platform
        self.settings = settings or TutorSettings()
        self.speech = SpeechService(self.settings)
        self.memory_repo = MemoryTutorRepo()

    @property
    def pronunciation_available(self) -> bool:
        return bool(
            self.settings.TUTOR_TTS_ENABLED
            and self.settings.ELEVENLABS_API_KEY.get_secret_value()
            and self.settings.TUTOR_TTS_MAX_CHARS_PER_DAY > 0
            and any(
                (
                    self.settings.TUTOR_TTS_CANTONESE_VOICE_ID,
                    self.settings.TUTOR_TTS_MANDARIN_VOICE_ID,
                    self.settings.TUTOR_TTS_JAPANESE_VOICE_ID,
                )
            )
            and isinstance(self.platform, VoicePlatform)
        )

    async def aclose(self) -> None:
        await self.speech.aclose()

    def repo(self, stores: Stores) -> TutorRepo | MemoryTutorRepo:
        return TutorRepo(stores.db) if stores.db is not None else self.memory_repo

    async def _get_profile(self, user_key: str, stores: Stores) -> TutorProfile | None:
        return await self.repo(stores).get(user_key)

    def tools(self) -> list[Tool]:
        async def get_tutor_profile(ctx: ToolContext) -> ToolResult:
            profile = await self._get_profile(ctx.user.ref.key, ctx.stores)
            command = ctx.command_form("tutor")
            if profile is None:
                if not ctx.is_dm:
                    profile_line = f"No tutor profile. Set one in a DM with {command}."
                else:
                    return ToolResult(
                        "No tutor profile. The person can set one with /tutor <language> "
                        "<level> [<explanation languages>]."
                    )
            else:
                profile_line = _profile_line(profile)
            if ctx.is_dm:
                return ToolResult(profile_line)
            topic = await self.repo(ctx.stores).get_topic(ctx.chat.key, ctx.thread_id or "")
            return ToolResult(
                "\n".join(
                    (
                        profile_line,
                        _topic_status_line(topic),
                        f"To make this group or topic a tutor topic, a group admin sends {command} "
                        f"here <language> <level> [<explanation languages>] (or {command} here for "
                        f"no default); {command} here auto makes it reply to every message. You "
                        "can't change this with a tool; tell the person the command.",
                    )
                )
            )

        tools = [
            Tool(
                "get_tutor_profile",
                "Return the requester's own language-tutor settings and, in a group, this topic's "
                "tutor status and how to change it.",
                _EMPTY_SCHEMA,
                get_tutor_profile,
            )
        ]
        if self.pronunciation_available:
            tools.append(self._pronunciation_tool())
        return tools

    def _pronunciation_tool(self) -> Tool:
        schema = {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Target-language word or short phrase only; 1–120 characters.",
                },
                "language": {
                    "type": "string",
                    "enum": ["Cantonese", "Mandarin Chinese", "Japanese"],
                    "description": "Explicit pronunciation language; select Cantonese and "
                    "Mandarin separately.",
                },
                "pronunciation": {
                    "type": "string",
                    "description": "Caption aid: toned Jyutping/Pinyin or kana; 1–240 "
                    "characters. Not spoken or sent to the provider.",
                },
                "pace": {
                    "type": "string",
                    "enum": ["normal", "slow"],
                    "description": "Defaults to normal. Use slow only on explicit request; "
                    "one clip per turn.",
                },
            },
            "required": ["text", "language", "pronunciation"],
            "additionalProperties": False,
        }

        async def pronounce(
            ctx: ToolContext, *, text: str, language: str, pronunciation: str, pace: str = "normal"
        ) -> ToolResult:
            name = "pronounce_tutor_text"
            if ctx.turn.tool_attempts.get(name, 0):
                return ToolResult.error("A pronunciation clip was already attempted this turn.")
            if not _valid_pronunciation_request(text, language, pronunciation, pace):
                return ToolResult.error("Invalid pronunciation request.")
            if not self.pronunciation_available or not await may_pronounce(self.access, ctx):
                return ToolResult.error("Pronunciation is unavailable here.")
            if ctx.turn.tool_attempts.get(name, 0):
                return ToolResult.error("A pronunciation clip was already attempted this turn.")
            ctx.turn.tool_attempts[name] = 1
            caption = f"{text}\n{pronunciation}\n{'Slow' if pace == 'slow' else 'Normal'}"
            try:
                caption_length = len(caption.encode("utf-16-le")) // 2
            except UnicodeEncodeError:
                return ToolResult.error("Invalid pronunciation request.")
            if caption_length > 1024:
                return ToolResult.error("Pronunciation caption is too long.")
            try:
                audio = await self.speech.synthesize(
                    text, language, user_key=ctx.user.ref.key, pace=pace
                )
                if not await may_pronounce(self.access, ctx):
                    return ToolResult.error("Pronunciation delivery is no longer allowed.")
                await ctx.platform.send_voice(
                    ctx.chat, "pronunciation.mp3", audio, caption=caption, thread_id=ctx.thread_id
                )
                return ToolResult("Pronunciation clip sent before the text response.")
            except SpeechError:
                return ToolResult.error("Could not generate the pronunciation clip.")
            except Exception as exc:
                log.warning("tutor pronunciation delivery failed error=%s", type(exc).__name__)
                return ToolResult.error("Could not deliver the pronunciation clip.")

        return Tool(
            "pronounce_tutor_text",
            "Generate one requested tutor pronunciation clip.",
            schema,
            pronounce,
            writes=False,
        )

    def tool_gates(self):
        from chatcore_tutor.gates import build_gates

        return build_gates(self)

    def skills(self):
        instructions = LANGUAGE_TUTOR_INSTRUCTIONS
        examples = (
            "correct my japanese sentence",
            "how do I say this in korean",
            "teach me some basic thai phrases",
            "is my grammar right in this french sentence",
            "quiz me on japanese vocabulary",
            "explain this japanese grammar point",
            "帮我纠正这句日语",
            "这个韩语语法怎么用",
            "教我几句简单的泰语",
            "can help me practise my japanese anot",
            "what's the difference between は and が",
        )
        keywords = (
            "grammar",
            "vocabulary",
            "vocab",
            "conjugate",
            "conjugation",
            "jlpt",
            "hsk",
            "topik",
            "cefr",
            "romaji",
            "furigana",
            "pinyin",
            "语法",
            "生词",
            "文法",
        )
        if self.pronunciation_available:
            instructions = _ACTIVE_LANGUAGE_TUTOR_INSTRUCTIONS
            instructions += " " + PRONUNCIATION_INSTRUCTIONS
            examples += ("say this phrase aloud in Japanese",)
            keywords += (
                "pronunciation",
                "pronounce",
                "aloud",
                "jyutping",
                "tones",
                "听发音",
                "発音",
            )
        return [
            Skill(
                "language_tutor",
                "Language practice: explanations, corrections and drills at the learner's level",
                examples,
                (
                    ("get_tutor_profile", "pronounce_tutor_text")
                    if self.pronunciation_available
                    else ("get_tutor_profile",)
                ),
                keywords=keywords,
                commands=("tutor",),
                max_steps=3,
                sticky_turns=3,
                instructions=instructions,
                context=self._skill_context,
                pin=self._skill_pin,
            )
        ]

    async def _skill_context(self, ctx: ToolContext) -> str | None:
        profile = await self._get_profile(ctx.user.ref.key, ctx.stores)
        topic = None
        if not ctx.is_dm:
            topic = await self.repo(ctx.stores).get_topic(ctx.chat.key, ctx.thread_id or "")
        if profile is not None and profile.active:
            target = lookup_language(profile.target)
            if target is None:
                return None
            romanisation = target.romanisation or "none"
            explanation = _context_explanation(
                profile.explain, "explain in the person's usual language"
            )
            context = (
                "Tutor profile (set by this person with /tutor): learning "
                f"{target.name} at CEFR {profile.level}; "
                f"{explanation}; "
                f"romanisation: {romanisation}. Follow it even when their message is in "
                f"{target.name}."
            )
            if topic is not None:
                context += " Everything in this topic is language practice."
            return context
        if topic is None:
            return None
        if topic.target is None:
            return (
                "This topic is for language practice. Infer the language being practised from the "
                "conversation and explain in the group's usual language. Everything in this "
                "topic is language practice."
            )
        target = lookup_language(topic.target)
        if target is None:
            return None
        explain = _context_explanation(topic.explain, "explain in the group's usual language")
        return (
            f"This topic is for language practice: {target.name} at CEFR {topic.level}; "
            f"{explain}; romanisation: {target.romanisation or 'none'}. "
            "Everything in this topic is language practice."
        )

    async def _skill_pin(self, ctx: ToolContext) -> bool:
        if ctx.is_dm:
            profile = await self._get_profile(ctx.user.ref.key, ctx.stores)
            return profile is not None and profile.active
        return await self.repo(ctx.stores).get_topic(ctx.chat.key, ctx.thread_id or "") is not None

    def action_handlers(self):
        return {"topic_set": self, "topic_clear": self, "topic_reply": self}

    async def auto_address(self, message: IncomingMessage, stores: Stores) -> bool:
        if message.is_dm:
            return False
        topic = await self.repo(stores).get_topic(message.chat.key, message.thread_id or "")
        readable = bool(message.text.strip()) or any(
            attachment.kind in {AttachmentKind.PHOTO, AttachmentKind.DOCUMENT}
            for attachment in message.attachments
        )
        return topic is not None and topic.auto_reply and readable

    async def _request_topic_change(
        self,
        ctx: CommandContext,
        *,
        target: str | None,
        level: str | None,
        explain: tuple[str, ...],
        auto_reply: bool | None = None,
    ) -> str | None:
        if not await is_group_manager(self.access, self.platform, ctx.chat, ctx.user.ref):
            log.warning(
                "tutor topic permission refused chat=%s user=%s", ctx.chat.key, ctx.user.ref.key
            )
            return "Only a group admin can change tutor topics."
        if ctx.confirm is None:
            return "Confirmation is unavailable; nothing changed."
        thread_key = ctx.thread_id or ""
        payload = {
            "chat_key": ctx.chat.key,
            "thread_key": thread_key,
            "target": target,
            "level": level,
            "explain": ",".join(explain) if explain else None,
            "auto_reply": auto_reply,
        }
        if target is None:
            summary = "Make this topic a tutor topic (no default language)"
        else:
            summary = f"Make this topic a tutor topic (default {target} {level}"
            if explain:
                summary += f", explanations in {_explanations_text(explain)}"
            summary += ")"
        if auto_reply is True:
            summary = summary[:-1] + "; replies to every message)"
        elif auto_reply is False:
            summary = summary[:-1] + "; replies only when tagged)"
        action = PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin="tutor",
            kind="topic_set",
            payload=payload,
            summary=summary,
            thread_id=ctx.thread_id,
        )
        await ctx.confirm.request(
            action, ctx.user, reply_to=ctx.message.ref if ctx.message else None
        )
        return None

    async def _request_topic_reply(self, ctx: CommandContext, auto_reply: bool) -> str | None:
        if not await is_group_manager(self.access, self.platform, ctx.chat, ctx.user.ref):
            log.warning(
                "tutor topic permission refused chat=%s user=%s", ctx.chat.key, ctx.user.ref.key
            )
            return "Only a group admin can change tutor topics."
        thread_key = ctx.thread_id or ""
        topic = await self.repo(ctx.stores).get_topic(ctx.chat.key, thread_key)
        if topic is None:
            return await self._request_topic_change(
                ctx,
                target=None,
                level=None,
                explain=(),
                auto_reply=auto_reply,
            )
        if ctx.confirm is None:
            return "Confirmation is unavailable; nothing changed."
        action = PendingAction.create(
            chat=ctx.chat,
            requester=ctx.user.ref,
            plugin="tutor",
            kind="topic_reply",
            payload={
                "chat_key": ctx.chat.key,
                "thread_key": thread_key,
                "auto_reply": auto_reply,
            },
            summary=(
                "Let the tutor reply to every message in this topic"
                if auto_reply
                else "Limit tutor replies in this topic to tagged messages"
            ),
            thread_id=ctx.thread_id,
        )
        await ctx.confirm.request(
            action, ctx.user, reply_to=ctx.message.ref if ctx.message else None
        )
        return None

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        rejected = "Nothing changed. Request the tutor topic change again."
        try:
            payload = action.payload
            if (
                action.plugin != "tutor"
                or action.kind not in {"topic_set", "topic_clear", "topic_reply"}
                or action.chat != ctx.chat
                or action.requester != ctx.user.ref
                or action.thread_id != ctx.thread_id
                or payload["chat_key"] != ctx.chat.key
                or payload["thread_key"] != (ctx.thread_id or "")
                or ctx.is_dm
            ):
                return rejected
            if not await is_group_manager(self.access, self.platform, ctx.chat, ctx.user.ref):
                return rejected
            if action.kind == "topic_reply":
                auto_reply = payload["auto_reply"]
                if type(auto_reply) is not bool:
                    return rejected
                topic = await self.repo(ctx.stores).get_topic(ctx.chat.key, ctx.thread_id or "")
                if topic is None:
                    return "This tutor topic no longer exists. Nothing changed."
                if not await self.repo(ctx.stores).set_topic_auto_reply(
                    ctx.chat.key, ctx.thread_id or "", auto_reply
                ):
                    return "This tutor topic no longer exists. Nothing changed."
                return (
                    "Tutor topic will reply to every message."
                    if auto_reply
                    else "Tutor topic will reply only when tagged."
                )
            if action.kind == "topic_clear":
                if any(payload.get(key) is not None for key in ("target", "level", "explain")):
                    return rejected
                await self.repo(ctx.stores).delete_topic(ctx.chat.key, ctx.thread_id or "")
                log.info(
                    "tutor topic cleared chat=%s thread=%s", ctx.chat.key, ctx.thread_id or "-"
                )
                return "Tutor mode stopped in this topic."
            target, level, explain = payload["target"], payload["level"], payload["explain"]
            auto_reply = payload["auto_reply"]
            if auto_reply is not None and type(auto_reply) is not bool:
                return rejected
            if target is None:
                if level is not None or explain is not None:
                    return rejected
            else:
                parsed = parse_tutor_args(" ".join(x for x in (target, level, explain or "") if x))
                expected_explain = ",".join(parsed.explain) if parsed.explain else None
                if (parsed.target, parsed.level, expected_explain) != (target, level, explain):
                    return rejected
                explain = parsed.explain or None
            await self.repo(ctx.stores).upsert_topic(
                ctx.chat.key,
                ctx.thread_id or "",
                target,
                level,
                explain,
                ctx.user.ref.key,
                auto_reply,
            )
            log.info("tutor topic set chat=%s thread=%s", ctx.chat.key, ctx.thread_id or "-")
            return "Tutor mode is on in this topic."
        except (KeyError, TypeError, ValueError):
            return rejected

    def commands(self) -> list[Command]:
        def error_reply(
            ctx: CommandContext, error: str, *, group: bool, manager: bool = False
        ) -> str | CommandReply:
            usage = _group_reply_usage(ctx) if group else _USAGE
            text = f"{error}\n{usage}\nReply to this message with a corrected value."
            if not group or manager:
                return CommandReply(
                    text, awaits_reply=True, placeholder="e.g. cantonese A1 english"
                )
            return text

        async def tutor(ctx: CommandContext, args: str) -> str | CommandReply | None:
            operation = args.strip().casefold()
            if ctx.is_dm and (operation == "here" or operation.startswith("here ")):
                return "This command marks a group topic; use it in a group. Nothing changed."
            if not ctx.is_dm and not operation:
                topic = await self.repo(ctx.stores).get_topic(ctx.chat.key, ctx.thread_id or "")
                lines = [_topic_status_line(topic)]
                command = ctx.command_form("tutor")
                manager = await is_group_manager(self.access, self.platform, ctx.chat, ctx.user.ref)
                if manager:
                    if topic is None:
                        lines.append(
                            f"To turn it on: {command} here <language> <level> "
                            f"[<explanation languages>], or {command} here for no default language."
                        )
                    else:
                        lines.append(
                            f"Change it with {command} here <language> <level> "
                            f"[<explanation languages>]; {command} here auto replies to every "
                            f"message, {command} here quiet only when tagged, {command} here off "
                            "turns it off."
                        )
                    lines.append(
                        'Or reply to this message with one of those, e.g. "japanese A2 english", '
                        '"auto" or "off".'
                    )
                else:
                    lines.append(
                        f"A group admin can turn it on with {command} here <language> <level>."
                    )
                lines.append(f"Set your own profile with {command} in a DM with me.")
                text = "\n".join(lines)
                if manager:
                    return CommandReply(
                        text, awaits_reply=True, placeholder="e.g. japanese A2 english"
                    )
                return text
            if not ctx.is_dm:
                auto_reply = None
                if len(args) > 200:
                    manager = await is_group_manager(
                        self.access, self.platform, ctx.chat, ctx.user.ref
                    )
                    return error_reply(
                        ctx, "Tutor settings are too long.", group=True, manager=manager
                    )
                if operation == "here" or operation.startswith("here "):
                    topic_args = args.strip()[4:].strip().casefold()
                    topic_tokens = topic_args.split()
                    if len(topic_tokens) > 1 and topic_tokens[-1] in {"auto", "quiet"}:
                        mark_args = " ".join(topic_tokens[:-1])
                        try:
                            parse_tutor_args(mark_args)
                        except ValueError:
                            pass
                        else:
                            auto_reply = topic_tokens[-1] == "auto"
                            topic_args = mark_args
                elif operation in {"on", "enable"}:
                    topic_args = "on"
                elif operation in {"off", "disable"}:
                    topic_args = "off"
                elif operation in {"auto", "quiet"}:
                    topic_args = operation
                else:
                    topic_tokens = args.strip().casefold().split()
                    topic_args = args.strip().casefold()
                    if len(topic_tokens) > 1 and topic_tokens[-1] in {"auto", "quiet"}:
                        mark_args = " ".join(topic_tokens[:-1])
                        try:
                            parse_tutor_args(mark_args)
                        except ValueError:
                            pass
                        else:
                            auto_reply = topic_tokens[-1] == "auto"
                            topic_args = mark_args
                    try:
                        parse_tutor_args(topic_args)
                    except ValueError as exc:
                        manager = await is_group_manager(
                            self.access, self.platform, ctx.chat, ctx.user.ref
                        )
                        return error_reply(ctx, str(exc), group=True, manager=manager)

                if topic_args in {"on", "enable"}:
                    if not await is_group_manager(
                        self.access, self.platform, ctx.chat, ctx.user.ref
                    ):
                        log.warning(
                            "tutor topic permission refused chat=%s user=%s",
                            ctx.chat.key,
                            ctx.user.ref.key,
                        )
                        return "Only a group admin can change tutor topics."
                    topic = await self.repo(ctx.stores).get_topic(ctx.chat.key, ctx.thread_id or "")
                    if topic is not None:
                        return _topic_status_line(topic)
                    return await self._request_topic_change(
                        ctx, target=None, level=None, explain=()
                    )
                if not topic_args:
                    return await self._request_topic_change(
                        ctx, target=None, level=None, explain=(), auto_reply=auto_reply
                    )
                if topic_args in {"off", "disable"}:
                    if not await is_group_manager(
                        self.access, self.platform, ctx.chat, ctx.user.ref
                    ):
                        log.warning(
                            "tutor topic permission refused chat=%s user=%s",
                            ctx.chat.key,
                            ctx.user.ref.key,
                        )
                        return "Only a group admin can change tutor topics."
                    if ctx.confirm is None:
                        return "Confirmation is unavailable; nothing changed."
                    action = PendingAction.create(
                        chat=ctx.chat,
                        requester=ctx.user.ref,
                        plugin="tutor",
                        kind="topic_clear",
                        payload={
                            "chat_key": ctx.chat.key,
                            "thread_key": ctx.thread_id or "",
                            "target": None,
                            "level": None,
                            "explain": None,
                        },
                        summary="Stop tutor mode in this topic",
                        thread_id=ctx.thread_id,
                    )
                    await ctx.confirm.request(
                        action, ctx.user, reply_to=ctx.message.ref if ctx.message else None
                    )
                    return None
                if topic_args in {"quiet", "auto"}:
                    return await self._request_topic_reply(ctx, topic_args == "auto")
                if topic_args in {"on", "enable", "off", "disable"}:
                    auto_reply = None
                try:
                    parsed = parse_tutor_args(topic_args)
                except ValueError as exc:
                    manager = await is_group_manager(
                        self.access, self.platform, ctx.chat, ctx.user.ref
                    )
                    return error_reply(ctx, str(exc), group=True, manager=manager)
                return await self._request_topic_change(
                    ctx,
                    target=parsed.target,
                    level=parsed.level,
                    explain=parsed.explain,
                    auto_reply=auto_reply,
                )
            if len(args) > 200:
                return error_reply(ctx, "Tutor settings are too long.", group=False)
            user_key = ctx.user.ref.key
            repo = self.repo(ctx.stores)
            existing = await repo.get(user_key)
            operation = args.strip().casefold()
            if not operation:
                text = f"{_profile_line(existing)}\n{_USAGE_HINT}" if existing else _USAGE
                return CommandReply(text, awaits_reply=True, placeholder="e.g. japanese A2 english")
            if operation == "off":
                if not await repo.set_active(user_key, False):
                    return error_reply(ctx, "No tutor profile is set.", group=False)
                log.info("tutor profile paused user=%s", user_key)
                return "Tutor is paused. Use /tutor on to resume."
            if operation == "on":
                if not await repo.set_active(user_key, True):
                    return error_reply(ctx, "No tutor profile is set.", group=False)
                log.info("tutor profile resumed user=%s", user_key)
                updated = await repo.get(user_key)
                return (
                    _profile_line(updated)
                    if updated
                    else error_reply(ctx, "No tutor profile is set.", group=False)
                )
            if operation == "clear":
                await repo.delete(user_key)
                log.info("tutor profile cleared user=%s", user_key)
                return "Tutor profile cleared."
            try:
                parsed = parse_tutor_args(args)
                explain = parsed.explain or (existing.explain if existing else ("English",))
                explain = tuple(language for language in explain if language != parsed.target)
                if not explain:
                    if parsed.target == "English":
                        raise ValueError('Give an explanation language different from "English".')
                    explain = ("English",)
            except ValueError as exc:
                return error_reply(ctx, str(exc), group=False)
            try:
                profile = await repo.upsert(user_key, parsed.target, parsed.level, explain)
            except ValueError as exc:
                return error_reply(ctx, str(exc), group=False)
            log.info("tutor profile set user=%s", user_key)
            return _profile_line(profile)

        return [Command("tutor", "Set private language-tutor preferences", tutor)]

    async def purge_user(self, chat: ChatRef, user: UserRef, stores: Stores) -> None:
        await self.repo(stores).delete(user.key)

    async def purge_chat(self, chat: ChatRef, stores: Stores) -> None:
        await self.repo(stores).purge_chat(chat.key)
