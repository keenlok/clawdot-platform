"""The human-confirmation flow for pending writes (§6a).

Buttons where the platform has them, otherwise a reply code (``YES 4821``). Either way only the
requester can confirm, each action is single use, and it expires after 10 minutes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from chatcore.events import ButtonPress, IncomingMessage, MessageRef, User
from chatcore.pending import PendingAction, PendingActionHandler
from chatcore.platform import ChatPlatform
from chatcore.plugin import CommandContext
from chatcore.store.base import CoreRepo

log = logging.getLogger(__name__)

EXPIRED = "That request has expired or was already handled. Ask again if you still want it."
CANCELLED = "Cancelled. Nothing was saved."
FAILED = "Something went wrong saving that. Please try again."


class ConfirmService:
    def __init__(
        self,
        platform: ChatPlatform,
        repo: CoreRepo,
        handlers: Mapping[str, PendingActionHandler],
    ) -> None:
        self.platform = platform
        self.repo = repo
        self.handlers = dict(handlers)

    async def commit_direct(self, action: PendingAction, user: User, ctx: CommandContext) -> str:
        """Commit an opted-in action without creating a pending confirmation row."""
        if action.requester != user.ref:
            return FAILED
        handler = self.handlers.get(action.handler_key)
        if handler is None:
            log.error("no handler for pending action %s", action.handler_key)
            return FAILED
        try:
            return await handler.commit(action, ctx)
        except Exception as exc:
            log.error("pending action %s failed: %s", action.handler_key, type(exc).__name__)
            return FAILED

    async def request(
        self, action: PendingAction, requester: User, *, reply_to: MessageRef | None = None
    ) -> MessageRef:
        """Store the action and ask the requester to confirm exactly ``action.summary``."""
        if action.handler_key not in self.handlers:
            raise LookupError(f"no handler for pending action {action.handler_key!r}")
        await self.repo.save_pending(action)
        if self.platform.capabilities.buttons:
            return await self.platform.confirm(
                action.chat,
                action.summary,
                action.id,
                action.requester,
                reply_to=reply_to,
                thread_id=action.thread_id,
            )
        prompt = (
            f"{action.summary}\n\n{requester.display_name}: reply YES {action.reply_code} to "
            f"confirm or NO {action.reply_code} to cancel. Expires in 10 minutes."
        )
        return await self.platform.send(
            action.chat, prompt, reply_to=reply_to, thread_id=action.thread_id
        )

    async def on_press(
        self,
        press: ButtonPress,
        ctx: CommandContext,
        *,
        action: PendingAction | None = None,
    ) -> str | None:
        if action is None:
            action = await self.repo.get_pending(press.chat, press.action_id)
        return await self._resolve(action, press.user, press.choice == "confirm", ctx)

    async def on_reply_code(
        self, message: IncomingMessage, confirmed: bool, code: str, ctx: CommandContext
    ) -> str | None:
        action = await self.repo.find_pending_by_code(message.chat, message.user.ref, code)
        return await self._resolve(action, message.user, confirmed, ctx)

    async def _resolve(
        self, action: PendingAction | None, user: User, confirmed: bool, ctx: CommandContext
    ) -> str | None:
        """Returns the text to post, or ``None`` to stay silent (unknown action, wrong user)."""
        if action is None or action.requester != user.ref:
            return None
        claimed = await self.repo.claim_pending(action.chat, action.id)
        if claimed is None:
            return EXPIRED
        if not confirmed:
            return CANCELLED
        handler = self.handlers.get(claimed.handler_key)
        if handler is None:
            log.error("no handler for pending action %s", claimed.handler_key)
            return FAILED
        try:
            return await handler.commit(claimed, ctx)
        except Exception as exc:
            log.error("pending action %s failed: %s", claimed.handler_key, type(exc).__name__)
            return FAILED
