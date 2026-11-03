"""When the bot responds (§3, §3c). Only humans start turns: bot messages are always ignored."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from chatcore.events import IncomingMessage
from chatcore.store.base import ChatSettings

_COMMAND = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]{3,64}))?(?:\s+(.*))?$", re.DOTALL)
_REPLY_CODE = re.compile(r"^\s*(yes|no)\s+(\d{4})\s*$", re.IGNORECASE)


class Route(StrEnum):
    IGNORE = "ignore"
    COMMAND = "command"
    RESPOND = "respond"
    REPLY_CODE = "reply_code"
    CHATMODE = "chatmode"
    """Not addressed to the bot, but ``/chatmode`` is on: a cheap classifier decides (track A)."""


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    args: str


def parse_command(text: str, bot_name: str) -> ParsedCommand | None:
    """``/name args`` or ``/name@bot args``. Commands addressed to another bot return ``None``."""
    match = _COMMAND.match(text.strip())
    if not match:
        return None
    name, target, args = match.groups()
    if target and target.lower() != bot_name.lower():
        return None
    return ParsedCommand(name.lower(), (args or "").strip())


def parse_reply_code(text: str) -> tuple[bool, str] | None:
    """``YES 4821`` / ``no 4821`` → ``(confirmed, code)``."""
    match = _REPLY_CODE.match(text)
    return (match[1].lower() == "yes", match[2]) if match else None


def strip_mention(text: str, bot_name: str) -> str:
    return re.sub(
        rf"^\s*@{re.escape(bot_name)}(?:\s+|$)",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    ).strip()


def addressed_command(message: IncomingMessage, bot_name: str) -> ParsedCommand | None:
    """Parse commands explicitly addressed to this bot, by username or leading mention."""
    command = parse_command(message.text, bot_name)
    if command is not None:
        return command
    mention = re.match(
        rf"^@{re.escape(bot_name)}(?:\s+|$)(.*)$", message.text, re.IGNORECASE | re.DOTALL
    )
    if message.mentions_bot and mention:
        return parse_command(mention[1], bot_name)
    return None


def decide(
    message: IncomingMessage,
    settings: ChatSettings,
    *,
    bot_name: str,
    group_mentions_only: bool = False,
) -> Route:
    if message.user.is_bot:
        return Route.IGNORE
    if parse_reply_code(message.text):
        return Route.REPLY_CODE
    command = parse_command(message.text, bot_name)
    if group_mentions_only and not message.is_dm:
        command = addressed_command(message, bot_name)
        first = message.text.strip().split(maxsplit=1)[0] if message.text.strip() else ""
        addressed_target = bool(
            re.fullmatch(rf"/[A-Za-z0-9_]{{1,32}}@{re.escape(bot_name)}", first, re.IGNORECASE)
        )
        leading_mention = bool(
            message.mentions_bot
            and re.match(rf"^@{re.escape(bot_name)}(?:\s+|$)", message.text, re.IGNORECASE)
        )
        if command is not None and (addressed_target or leading_mention):
            return Route.RESPOND if command.name == "ask" else Route.COMMAND
        if re.match(
            r"^/[A-Za-z0-9_]{1,32}(?:@[A-Za-z0-9_]{3,64})?(?:\s|$)",
            message.text.strip(),
        ) and not re.search(rf"@{re.escape(bot_name)}\b", message.text, re.IGNORECASE):
            return Route.IGNORE
        if not message.mentions_bot:
            return Route.RESPOND if message.reply_to_bot else Route.IGNORE
    if command:
        return Route.RESPOND if command.name == "ask" else Route.COMMAND
    if message.text.startswith("/"):
        return Route.IGNORE  # a command for another bot
    if message.is_dm or message.mentions_bot or message.reply_to_bot:
        return Route.RESPOND
    if settings.chatmode and message.text.strip():
        return Route.CHATMODE
    return Route.IGNORE


def prompt_text(message: IncomingMessage, bot_name: str) -> str:
    """The user's request with ``/ask`` and the bot mention removed."""
    command = addressed_command(message, bot_name)
    if command and command.name == "ask":
        return command.args
    if message.mentions_bot:
        return strip_mention(message.text, bot_name)
    return message.text.strip()
