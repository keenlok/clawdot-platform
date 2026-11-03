"""``/model``: switch this chat's model for a while (``plans/model-switch.md``).

Only the command switches. The read-only ``get_model_info`` tool lets the bot answer "which model
are you?" and point people to ``/model``; no tool can write the override.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime

from chatcore.access import AccessPolicy
from chatcore.command_prompts import CommandReply
from chatcore.display_time import format_instant
from chatcore.display_time_store import display_service
from chatcore.events import ChatRef, UserRef
from chatcore.model_choice import ModelChoice, ModelSwitchConfig
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.skills import Skill
from chatcore.store.base import Stores
from chatcore.tools.registry import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)

_DEFAULT_WORDS = frozenset({"default", "reset"})
_TRAINING_NOTICE = (
    "Heads up: while this switch is on, {name}'s provider uses the prompts and replies in this "
    "chat to train its models, so don't share anything private."
)

BOT_MODEL_INSTRUCTIONS = (
    "Use get_model_info for the current model, temporary override, expiry and available choices; "
    "never infer these from conversation history. The tool is read-only: people switch models "
    "with /model, and the assistant cannot switch one itself. Relay the command form, expiry, "
    "vision limits and provider training notices returned by the tool. Never claim a switch "
    "happened without a successful command result."
)


def _label(choice: ModelChoice) -> str:
    labels = []
    if not choice.vision:
        labels.append("text only")
    if choice.trains_on_input:
        labels.append("trains on chats")
    suffix = f" ({', '.join(labels)})" if labels else ""
    return f"{choice.name}{suffix}"


def _training_notice(choice: ModelChoice) -> str:
    return _TRAINING_NOTICE.format(name=choice.name)


def _wait(until: datetime, now: datetime) -> str:
    minutes = max(1, math.ceil((until - now).total_seconds() / 60))
    return f"{minutes} minute" + ("" if minutes == 1 else "s")


class ModelPlugin(BasePlugin):
    name = "model"
    help_title = "Model"

    def __init__(
        self,
        config: ModelSwitchConfig,
        access: AccessPolicy | None = None,
        *,
        bot_name: str = "",
        image_fallback: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.access = access
        self.bot_name = bot_name
        self.image_fallback = image_fallback
        self.clock = clock or (lambda: datetime.now(UTC))

    # -- shared ----------------------------------------------------------------------------

    def _form(self, is_dm: bool) -> str:
        return f"/model@{self.bot_name}" if self.bot_name and not is_dm else "/model"

    def _choices_text(self) -> str:
        return ", ".join(_label(choice) for choice in self.config.choices)

    async def _until_text(
        self, stores: Stores, chat: ChatRef, user: UserRef, is_dm: bool, until: datetime
    ) -> str:
        zone, _ = await display_service(stores).resolve(chat, user, is_dm=is_dm)
        return format_instant(until, zone)

    async def _may_switch(self, ctx: CommandContext) -> bool:
        if self.config.anyone or ctx.is_admin:
            return True
        if ctx.is_dm:
            return False
        if self.access is not None:
            try:
                await self.access.refresh()
                if self.access.can_setup(ctx.user.ref, ctx.chat):
                    return True
            except Exception as exc:
                log.warning("model switch access refresh failed error=%s", type(exc).__name__)
        native_check = getattr(ctx.platform, "is_group_admin", None)
        if not callable(native_check) or ctx.platform.name != ctx.chat.platform:
            return False
        try:
            return bool(await native_check(ctx.chat, ctx.user.ref))
        except Exception:
            return False

    # -- command ---------------------------------------------------------------------------

    def commands(self) -> list[Command]:
        return [Command("model", "Show or switch this chat's model for a while", self._command)]

    async def _command(self, ctx: CommandContext, args: str) -> str | CommandReply:
        pieces = args.split()
        if not pieces:
            return CommandReply(
                await self._show(ctx) + "\nOr reply to this message with a model name, or default.",
                awaits_reply=True,
                placeholder="Model name or default",
            )
        if len(pieces) != 1:
            form = self._form(ctx.is_dm)
            return f"Use {form}, {form} <name> or {form} default."
        word = pieces[0]
        if word.casefold() in _DEFAULT_WORDS or word.casefold() == (
            self.config.default_model.casefold()
        ):
            return await self._clear(ctx)
        choice = self.config.choice(word)
        if choice is None:
            return CommandReply(
                f"Unknown model. Choose one of: {self._choices_text()}, or default. "
                "Reply to this message with one.",
                awaits_reply=True,
                placeholder="Model name or default",
            )
        if not await self._may_switch(ctx):
            return "Only group admins and setup managers can switch the model here."
        return await self._switch(ctx, choice)

    async def _show(self, ctx: CommandContext) -> str:
        active = await self.config.active(ctx.chat)
        lines = []
        if active is None:
            lines.append(f"Model here: {self.config.default_model} (default).")
        else:
            override, choice = active
            until = await self._until_text(
                ctx.stores, ctx.chat, ctx.user.ref, ctx.is_dm, override.until
            )
            lines.append(
                f"Model here: {_label(choice)}, switched by {override.set_by} until {until}. "
                f"Default: {self.config.default_model}."
            )
            if choice.trains_on_input:
                lines.append(_training_notice(choice))
        lines.append(f"Choices: {self._choices_text()}.")
        minutes = int(self.config.store.duration.total_seconds() // 60)
        form = self._form(ctx.is_dm)
        lines.append(
            f"{form} <name> switches this chat for {minutes} minutes; {form} default switches back."
        )
        return "\n".join(lines)

    async def _switch(self, ctx: CommandContext, choice: ModelChoice) -> str:
        result = await self.config.store.switch(
            ctx.chat, choice.name, set_by=ctx.user.display_name or "someone"
        )
        now = self.clock()
        if result.reason == "already" and result.override is not None:
            until = await self._until_text(
                ctx.stores, ctx.chat, ctx.user.ref, ctx.is_dm, result.override.until
            )
            if choice.trains_on_input:
                form = self._form(ctx.is_dm)
                return (
                    f"This chat is already on {_label(choice)} until {until}. "
                    f"{_training_notice(choice)} {form} default switches back now."
                )
            return f"This chat is already on {_label(choice)} until {until}."
        if result.reason == "cooldown" and result.retry_at is not None:
            return f"The model was switched recently. Try again in {_wait(result.retry_at, now)}."
        if result.reason == "cap" and result.retry_at is not None:
            until = await self._until_text(
                ctx.stores, ctx.chat, ctx.user.ref, ctx.is_dm, result.retry_at
            )
            maximum = int(self.config.store.max_used.total_seconds() // 60)
            return (
                f"This chat has used its {maximum} minutes of switched models in the last "
                f"3 hours. Try again after {until}."
            )
        override = result.override
        assert override is not None
        until = await self._until_text(
            ctx.stores, ctx.chat, ctx.user.ref, ctx.is_dm, override.until
        )
        log.info(
            "model override set chat=%s model=%s until=%s",
            ctx.chat.key,
            choice.name,
            override.until.isoformat(),
        )
        granted = _wait(override.until, now)
        note = self._image_note(choice)
        training = f" {_training_notice(choice)}" if choice.trains_on_input else ""
        return (
            f"Switched this chat to {choice.name} for {granted} (set by {override.set_by}, "
            f"reverts {until}).{note}{training} {self._form(ctx.is_dm)} default switches back now."
        )

    def _image_note(self, choice: ModelChoice) -> str:
        if choice.vision:
            return ""
        if self.image_fallback:
            return f" Images are described by {self.image_fallback} while it's on."
        return " Images aren't read while it's on."

    async def _clear(self, ctx: CommandContext) -> str:
        previous = await self.config.store.clear(ctx.chat)
        if previous is None:
            return f"This chat is already on the default model, {self.config.default_model}."
        log.info("model override cleared chat=%s model=%s", ctx.chat.key, previous.model)
        return f"Switched this chat back to the default model, {self.config.default_model}."

    # -- read-only tool and skill ----------------------------------------------------------

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "get_model_info",
                "Show which language model answers in this chat, whether a temporary /model "
                "switch is on and until when, whether the current model's provider trains on chat "
                "content, and the models people can switch to. Read-only: "
                "you can't switch models yourself; people use the /model command.",
                {"type": "object", "additionalProperties": False, "properties": {}},
                self._get_model_info,
            )
        ]

    async def _get_model_info(self, ctx: ToolContext) -> ToolResult:
        command = ctx.command_form("model") if callable(ctx.command_form) else "/model"
        active = await self.config.active(ctx.chat)
        training_detail = ""
        if any(choice.trains_on_input for choice in self.config.choices):
            training_detail = (
                " Choices marked (trains on chats) are run by providers that use prompts and "
                "replies to train their models; the current model isn't marked that way."
            )
        if active is None:
            current = f"Current model: {self.config.default_model} (the default)."
        else:
            override, choice = active
            until = await self._until_text(
                ctx.stores, ctx.chat, ctx.user.ref, ctx.is_dm, override.until
            )
            current = (
                f"Current model: {_label(choice)}, a temporary switch until {until}. "
                f"Default: {self.config.default_model}."
            )
            if choice.trains_on_input:
                training_detail = (
                    f" The provider of the current model uses prompts and replies in this chat to "
                    f"train its models while the switch is on, so it isn't safe to share anything "
                    f"private here now. {command} default switches back."
                )
            if self.image_fallback and not choice.vision:
                current += self._image_note(choice)
        return ToolResult(
            f"{current}{training_detail} Choices: {self._choices_text()}. "
            "You can't change the model yourself. "
            f"To switch, the person sends {command} <name>; {command} default switches back."
        )

    def skills(self) -> list[Skill]:
        return [
            Skill(
                "bot_model",
                "Which language model answers here, and how people switch it with /model",
                (
                    "which model are you using",
                    "can you switch to a smarter model",
                    "change your model to something else",
                    "what AI model is this",
                    "你用的是什么模型",
                    "可以换个模型吗",
                    "can change model or not",
                    "which model you running ah",
                ),
                ("get_model_info",),
                keywords=("model", "模型", "llm"),
                commands=("model",),
                max_steps=1,
                instructions=BOT_MODEL_INSTRUCTIONS,
            )
        ]
