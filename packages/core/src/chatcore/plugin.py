"""The plugin interface (§3d). Apps list the plugins they want explicitly; nothing auto-loads."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from importlib.resources.abc import Traversable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from chatcore.events import ChatRef, IncomingMessage, User, UserRef
from chatcore.pending import PendingActionHandler
from chatcore.platform import ChatPlatform
from chatcore.skills import Skill, ToolGate
from chatcore.store.base import Stores
from chatcore.tools.registry import Tool, plain_command_form

if TYPE_CHECKING:
    from chatcore.command_prompts import CommandReply
    from chatcore.confirm import ConfirmService


@dataclass(slots=True)
class CommandContext:
    chat: ChatRef
    user: User
    is_dm: bool
    is_admin: bool
    platform: ChatPlatform
    stores: Stores
    thread_id: str | None = None
    message: IncomingMessage | None = None
    confirm: ConfirmService | None = None
    """For commands that write: ``await ctx.confirm.request(action, ctx.user)``."""
    command_form: Callable[[str], str] = plain_command_form
    """How to spell a slash command here (``/name@bot`` in mention-only groups)."""


CommandHandler = Callable[[CommandContext, str], Awaitable["str | CommandReply | None"]]


@dataclass(frozen=True, slots=True)
class Command:
    """A slash command that runs without the LLM. The handler gets the text after the command
    and returns the reply (or ``None`` for no reply). Returning
    ``CommandReply(text, awaits_reply=True)`` makes a reply to that message the next input for
    the same command (``chatcore.command_prompts``)."""

    name: str
    description: str
    handler: CommandHandler
    admin_only: bool = False
    """Non-admins get no reply at all (§6a)."""


@runtime_checkable
class Plugin(Protocol):
    name: str

    def migrations(self) -> Traversable | None:
        """Directory of ``NNNN_*.sql`` files, applied in order and recorded per plugin."""

    def tools(self) -> list[Tool]: ...
    def commands(self) -> list[Command]: ...
    def action_handlers(self) -> dict[str, PendingActionHandler]:
        """Handlers for this plugin's pending actions, keyed by ``PendingAction.kind``."""

    async def on_startup(self, stores: Stores) -> None: ...

    async def on_bot_added(self, chat: ChatRef, platform: ChatPlatform, stores: Stores) -> None:
        """Run once for an allowlisted bot-added event, e.g. to post the privacy notice."""

    async def on_message(self, message: IncomingMessage, stores: Stores) -> None:
        """Called for every logged message (e.g. to index it). Must not reply."""

    async def auto_address(self, message: IncomingMessage, stores: Stores) -> bool:
        """Return True to treat an untagged group message as addressed to the bot. Called only
        for human, non-command, non-empty group messages that routing would otherwise ignore."""

    async def purge_user(self, chat: ChatRef, user: UserRef, stores: Stores) -> None:
        """``/forgetme``: hard-delete this user's data scoped to the required chat (§6a)."""

    async def purge_chat(self, chat: ChatRef, stores: Stores) -> None:
        """``/purgechat``: hard-delete this plugin's data scoped to the required chat."""

    async def purge_older_than(self, cutoff: datetime, stores: Stores) -> None:
        """Retention hook: hard-delete this plugin's data older than ``cutoff``."""


class BasePlugin:
    """No-op defaults. Subclasses set ``name`` and override what they need."""

    name: str = ""
    explainer_package: str | None = None
    """Optional import package containing this plugin's public ``EXPLAIN.md`` resource."""
    help_title: str = ""
    """Heading for this plugin's commands in ``/help``; defaults to a title-cased ``name``."""
    migrations_package: str | None = None
    """Importable package holding the ``.sql`` files, e.g. ``"chatcore_expenses.migrations"``."""

    def migrations(self) -> Traversable | None:
        return resources.files(self.migrations_package) if self.migrations_package else None

    def tools(self) -> list[Tool]:
        return []

    def skills(self) -> list[Skill]:
        return []

    def tool_gates(self) -> dict[str, ToolGate]:
        return {}

    def commands(self) -> list[Command]:
        return []

    def action_handlers(self) -> dict[str, PendingActionHandler]:
        return {}

    async def on_startup(self, stores: Stores) -> None:
        return None

    async def on_bot_added(self, chat: ChatRef, platform: ChatPlatform, stores: Stores) -> None:
        return None

    async def on_message(self, message: IncomingMessage, stores: Stores) -> None:
        return None

    async def auto_address(self, message: IncomingMessage, stores: Stores) -> bool:
        """Return True to treat an untagged group message as addressed to the bot. Called only
        for human, non-command, non-empty group messages that routing would otherwise ignore."""
        return False

    async def purge_user(self, chat: ChatRef, user: UserRef, stores: Stores) -> None:
        return None

    async def purge_chat(self, chat: ChatRef, stores: Stores) -> None:
        return None

    async def purge_older_than(self, cutoff: datetime, stores: Stores) -> None:
        return None
