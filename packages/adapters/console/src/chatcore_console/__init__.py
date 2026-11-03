"""Terminal chat adapter: a local "chat app" for development and end-to-end tests (§3d).

Type messages as the current user. Lines starting with ``::`` control the simulation:

    ::as <name>          speak as another user (created on first use)
    ::chat <id>          switch to group chat <id>
    ::dm                 switch to a DM with the bot
    ::reply [n]          your next message replies to the nth most recent bot message (default 1)
    ::press <id> yes|no  press Confirm/Cancel on a pending action
    ::quit               stop

Mention the bot with ``@<bot_name>``.
"""

from __future__ import annotations

import asyncio
import itertools
import sys
from collections.abc import AsyncIterator
from typing import TextIO

from chatcore.events import (
    Attachment,
    ButtonPress,
    ChatRef,
    IncomingMessage,
    MessageRef,
    User,
    UserRef,
)
from chatcore.platform import Capabilities, EventHandler, PlatformError

PLATFORM = "console"


async def _stdin_lines() -> AsyncIterator[str]:
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            return
        yield line.rstrip("\n")


class ConsolePlatform:
    name = PLATFORM
    capabilities = Capabilities(
        buttons=True, edit=True, threads=False, files=False, dm=True, max_message_len=4000
    )

    def __init__(
        self,
        *,
        bot_name: str,
        group_id: str = "group",
        user_name: str = "me",
        lines: AsyncIterator[str] | None = None,
        out: TextIO | None = None,
    ) -> None:
        self.bot_name = bot_name
        self._lines = lines
        self._out = out or sys.stdout
        self._ids = itertools.count(1)
        self._group = ChatRef(PLATFORM, group_id)
        self._user = self._make_user(user_name)
        self._dm = False
        self._reply_target: MessageRef | None = None
        self._last_bot_message: MessageRef | None = None
        self._bot_messages: list[MessageRef] = []
        self._running = False

    @staticmethod
    def _make_user(name: str) -> User:
        return User(UserRef(PLATFORM, name), display_name=name, username=name)

    def _chat(self) -> ChatRef:
        return ChatRef(PLATFORM, self._user.ref.id) if self._dm else self._group

    def _print(self, text: str) -> None:
        self._out.write(text + "\n")
        self._out.flush()

    # -- ChatPlatform ------------------------------------------------------------------------

    async def start(self, handler: EventHandler) -> None:
        self._running = True
        lines = self._lines or _stdin_lines()
        async for line in lines:
            if not self._running:
                break
            if line.startswith("::"):
                if await self._control(line[2:].strip(), handler) is False:
                    break
                continue
            if not line.strip():
                continue
            reply_to = self._reply_target
            self._reply_target = None
            chat = self._chat()
            await handler(
                IncomingMessage(
                    chat=chat,
                    message_id=str(next(self._ids)),
                    user=self._user,
                    text=line,
                    is_dm=self._dm,
                    reply_to=reply_to,
                    reply_to_bot=reply_to is not None,
                    mentions_bot=f"@{self.bot_name}".lower() in line.lower(),
                )
            )
        self._running = False

    async def _control(self, command: str, handler: EventHandler) -> bool | None:
        verb, _, arg = command.partition(" ")
        arg = arg.strip()
        match verb:
            case "as" if arg:
                self._user = self._make_user(arg)
            case "chat" if arg and arg != "*" and ":" not in arg:
                self._group, self._dm = ChatRef(PLATFORM, arg), False
            case "dm":
                self._dm = True
            case "reply":
                try:
                    index = int(arg or "1")
                except ValueError:
                    index = 0
                if index < 1 or index > len(self._bot_messages):
                    self._print("(no such bot message)")
                else:
                    self._reply_target = self._bot_messages[-index]
            case "press" if arg:
                action_id, _, choice = arg.partition(" ")
                await handler(
                    ButtonPress(
                        chat=self._chat(),
                        user=self._user,
                        action_id=action_id,
                        choice="confirm"
                        if choice.strip().lower() in ("", "y", "yes")
                        else "cancel",
                    )
                )
            case "quit":
                return False
            case _:
                self._print("(unknown control; see the module docstring)")
        return None

    async def stop(self) -> None:
        self._running = False

    async def send(
        self,
        chat: ChatRef,
        text: str,
        *,
        reply_to: MessageRef | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        ref = MessageRef(chat, f"b{next(self._ids)}", thread_id)
        where = "dm" if chat.id == self._user.ref.id else chat.id
        limit = self.capabilities.max_message_len
        for start in range(0, max(len(text), 1), limit):
            self._print(f"[{where}] {self.bot_name}: {text[start : start + limit]}")
        self._last_bot_message = ref
        self._bot_messages.append(ref)
        return ref

    async def edit(self, message: MessageRef, text: str) -> None:
        self._print(f"[{message.chat.id}] {self.bot_name} (edited {message.message_id}): {text}")

    async def typing(self, chat: ChatRef, thread_id: str | None = None) -> None:
        return None

    async def confirm(
        self,
        chat: ChatRef,
        prompt: str,
        action_id: str,
        requester: UserRef,
        *,
        reply_to: MessageRef | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        return await self.send(
            chat,
            f"{prompt}\n  [Confirm] [Cancel]  (type ::press {action_id} yes|no as {requester.id})",
            reply_to=reply_to,
            thread_id=thread_id,
        )

    async def download(self, attachment: Attachment, *, chat: ChatRef, max_bytes: int) -> bytes:
        raise PlatformError("the console adapter has no file attachments")

    async def is_member(self, chat: ChatRef, user: UserRef) -> bool:
        return True  # everyone at the terminal is in every simulated group

    async def leave(self, chat: ChatRef) -> None:
        self._print(f"[{chat.id}] ({self.bot_name} left the chat)")
