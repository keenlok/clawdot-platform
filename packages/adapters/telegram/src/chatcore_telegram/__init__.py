"""Telegram implementation of the platform boundary."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from chatcore.events import (
    Attachment,
    AttachmentKind,
    BotAddedToChat,
    ButtonPress,
    ChatRef,
    Event,
    IncomingMessage,
    MemberJoined,
    MemberLeft,
    Mention,
    MessageRef,
    PollAnswer,
    User,
    UserRef,
)
from chatcore.files import PhotoAlbumPlatform, PhotoPlatform, PhotoReceipt, VoicePlatform
from chatcore.images import error_label
from chatcore.platform import Capabilities, EventHandler, PlatformError
from chatcore.polls import PollLimits, PollResult, SentPoll
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    LinkPreviewOptions,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatAction, PollLimit
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import Application, TypeHandler
from telegram.request import BaseRequest

from chatcore_telegram.formatting import render_markdown, split_message, utf16_length
from chatcore_telegram.menu import fit_menu

NAME = "tg"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_VOICE_BYTES = 8 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
_VOICE_FILENAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\.mp3")
_PHOTO_FILENAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\.(png|jpg|jpeg)")
_COMMAND_NAME = re.compile(r"^[a-z0-9_]{1,32}$")
logger = logging.getLogger(__name__)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


def _image_size(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return (width, height) if width and height else None
    if not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            return None
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(data):
            return None
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2 or index + length > len(data):
            return None
        if marker in {0xC0, 0xC2} and length >= 7:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return (width, height) if width and height else None
        index += length
    return None


class TelegramSettings(BaseSettings):
    """Keep the bot token wrapped so accidental settings output redacts it."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)
    bot_token: SecretStr = Field(validation_alias="TELEGRAM_BOT_TOKEN")
    download_retries: int = Field(
        default=3, ge=0, le=5, validation_alias="TELEGRAM_DOWNLOAD_RETRIES"
    )
    download_backoff_s: float = Field(
        default=0.5, ge=0, le=5, validation_alias="TELEGRAM_DOWNLOAD_BACKOFF_S"
    )
    download_timeout_s: float = Field(
        default=30.0, gt=0, le=120, validation_alias="TELEGRAM_DOWNLOAD_TIMEOUT_S"
    )


class TelegramPlatform(VoicePlatform, PhotoPlatform, PhotoAlbumPlatform):
    """Bridge Telegram updates and methods to the platform independent core."""

    name = NAME
    poll_limits = PollLimits(
        PollLimit.MAX_OPTION_NUMBER, PollLimit.MAX_QUESTION_LENGTH, PollLimit.MAX_OPTION_LENGTH
    )
    capabilities = Capabilities(
        buttons=True,
        edit=True,
        threads=True,
        files=True,
        dm=True,
        max_message_len=4096,
        markup="markdown",
        pin=True,
    )

    def __init__(self, settings: TelegramSettings, *, request: BaseRequest | None = None) -> None:
        self._settings = settings
        builder = (
            Application.builder()
            .token(settings.bot_token.get_secret_value())
            .concurrent_updates(True)
        )
        if request is not None:
            builder = builder.request(request)
        self._application: Application = builder.build()
        self._handler: EventHandler | None = None
        self._stopped = asyncio.Event()
        self._bot_id: int | None = None
        self._bot_username: str | None = None
        self._private_commands: list[tuple[str, str]] = []
        self._group_commands: list[tuple[str, str]] = []
        self._sent_parts: dict[tuple[str, str], tuple[str, ...]] = {}

    def set_menu_commands(
        self,
        private: Sequence[tuple[str, str]],
        group: Sequence[tuple[str, str]],
    ) -> None:
        def valid(items):
            return [
                (name, description[:256])
                for name, description in items
                if isinstance(name, str)
                and _COMMAND_NAME.fullmatch(name)
                and isinstance(description, str)
            ]

        self._private_commands = fit_menu(valid(private), scope="private")
        self._group_commands = fit_menu(valid(group), scope="group")

    async def start(self, handler: EventHandler) -> None:
        """Run long polling in the caller's loop and release PTB resources on stop."""
        self._handler = handler
        app = self._application
        app.add_handler(TypeHandler(Update, self._handle_update))
        app.add_error_handler(self._handle_error)
        initialized = False
        try:
            await self._safe(app.initialize)
            initialized = True
            me = await self._safe(app.bot.get_me)
            self._bot_id, self._bot_username = me.id, me.username
            for commands, scope in (
                (self._private_commands, BotCommandScopeAllPrivateChats()),
                (self._group_commands, BotCommandScopeAllGroupChats()),
            ):
                try:
                    await app.bot.set_my_commands(
                        [BotCommand(name, description) for name, description in commands],
                        scope=scope,
                    )
                except Exception as exc:
                    logger.warning("Telegram command menu setup failed: %s", type(exc).__name__)
            await self._safe(app.start)
            await self._safe(
                app.updater.start_polling,
                allowed_updates=[
                    "message",
                    "callback_query",
                    "my_chat_member",
                    "chat_member",
                    "poll_answer",
                ],
            )
            await self._stopped.wait()
        finally:
            if app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            if initialized:
                await app.shutdown()

    async def stop(self) -> None:
        self._stopped.set()

    @staticmethod
    async def _handle_error(update: object, context: Any) -> None:
        """Keep PTB handler failures useful without exposing exception contents."""
        error = context.error
        logger.error("Telegram update handler failed: %s", type(error).__name__)

    async def _handle_update(self, update: Update, _context: Any = None) -> None:
        handler = self._handler
        if handler is None:
            return
        if getattr(update, "poll_answer", None):
            answer = update.poll_answer
            if answer.user is not None:
                await handler(
                    PollAnswer(answer.poll_id, self._user(answer.user), tuple(answer.option_ids))
                )
            return
        if update.callback_query:
            query = update.callback_query
            event: Event | None = None
            data = query.data
            if isinstance(data, str) and len(data) > 2 and data[1] == ":" and data[0] in "cx":
                user = self._user(query.from_user)
                message_ref = self._message_ref(query.message) if query.message else None
                chat = (
                    message_ref.chat
                    if message_ref
                    else (ChatRef(NAME, str(query.message.chat.id)) if query.message else None)
                )
                if chat is not None:
                    event = ButtonPress(
                        chat=chat,
                        user=user,
                        action_id=data[2:],
                        choice="confirm" if data[0] == "c" else "cancel",
                        message=message_ref,
                    )
            try:
                await query.answer()
            except TelegramError as exc:
                logger.warning("Telegram callback answer failed: %s", type(exc).__name__)
            if event is not None:
                await handler(event)
            return

        if update.message:
            message = update.message
            chat = ChatRef(NAME, str(message.chat.id))
            if message.new_chat_members:
                for member in message.new_chat_members:
                    await handler(MemberJoined(chat, self._user(member)))
            if message.left_chat_member:
                await handler(MemberLeft(chat, self._user(message.left_chat_member)))
            event = await self._map_message(message)
            if event is not None:
                await handler(event)
            return

        if update.chat_member:
            update_member = update.chat_member
            old_status = update_member.old_chat_member.status
            new = update_member.new_chat_member
            status = new.status
            old_member = self._is_member_status(
                old_status, getattr(update_member.old_chat_member, "is_member", None)
            )
            new_member = self._is_member_status(status, getattr(new, "is_member", None))
            if not old_member and new_member:
                await handler(
                    MemberJoined(ChatRef(NAME, str(update_member.chat.id)), self._user(new.user))
                )
            elif old_member and not new_member:
                await handler(
                    MemberLeft(ChatRef(NAME, str(update_member.chat.id)), self._user(new.user))
                )
            return

        if update.my_chat_member:
            change = update.my_chat_member
            old = change.old_chat_member
            new = change.new_chat_member
            became_member = not self._is_member_status(
                old.status, getattr(old, "is_member", None)
            ) and self._is_member_status(new.status, getattr(new, "is_member", None))
            if (
                became_member
                and new.user.id == self._bot_id
                and change.chat.type in {"group", "supergroup"}
            ):
                update_id = getattr(update, "update_id", None)
                await handler(
                    BotAddedToChat(
                        ChatRef(NAME, str(change.chat.id)),
                        self._user(change.from_user) if change.from_user else None,
                        f"{self._bot_id}:{update_id}" if type(update_id) is int else None,
                    )
                )

    async def _map_message(self, message: Any) -> IncomingMessage | None:
        if not self._has_content(message) or message.from_user is None:
            return None
        if self._bot_id is None:
            me = await self._safe(self._application.bot.get_me)
            self._bot_id, self._bot_username = me.id, me.username
        chat = ChatRef(NAME, str(message.chat.id))
        thread_id = str(message.message_thread_id) if message.is_topic_message else None
        reply = message.reply_to_message
        if reply and getattr(reply, "forum_topic_created", None):
            reply = None
        attachments = self._attachments(message)
        content = message.text or message.caption or ""
        mentions, mentions_bot = self._mention_data(message, content)
        return IncomingMessage(
            chat=chat,
            message_id=str(message.message_id),
            user=self._user(message.from_user),
            text=content,
            is_dm=message.chat.type == "private",
            thread_id=thread_id,
            attachments=tuple(attachments),
            reply_to=self._message_ref(reply) if reply else None,
            reply_to_bot=bool(reply and reply.from_user and reply.from_user.id == self._bot_id),
            mentions_bot=mentions_bot,
            sent_at=message.date,
            chat_title=message.chat.title if message.chat.type in ("group", "supergroup") else None,
            mentions=mentions,
        )

    @staticmethod
    def _has_content(message: Any) -> bool:
        return bool(
            message.text
            or message.caption
            or message.photo
            or message.document
            or message.audio
            or message.video
            or message.voice
        )

    @staticmethod
    def _entity_span(entity: Any, content: str) -> tuple[int, int, str] | None:
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        if (
            isinstance(offset, bool)
            or isinstance(length, bool)
            or not isinstance(offset, int)
            or not isinstance(length, int)
            or offset < 0
            or length <= 0
        ):
            return None
        try:
            encoded = content.encode("utf-16-le")
        except UnicodeEncodeError:
            return None
        start_byte, end_byte = offset * 2, (offset + length) * 2
        if start_byte >= end_byte or end_byte > len(encoded):
            return None
        try:
            start = len(encoded[:start_byte].decode("utf-16-le"))
            label = encoded[start_byte:end_byte].decode("utf-16-le")
        except UnicodeDecodeError:
            return None
        end = start + len(label)
        if content[start:end] != label:
            return None
        return start, end, label

    def _mention_data(self, message: Any, content: str) -> tuple[tuple[Mention, ...], bool]:
        if getattr(message, "text", None):
            entities = getattr(message, "entities", None) or []
        else:
            entities = getattr(message, "caption_entities", None) or []
        mentions: list[Mention] = []
        mentions_bot = False
        for entity in entities:
            kind = getattr(entity, "type", None)
            if kind not in {"mention", "text_mention"}:
                continue
            span = self._entity_span(entity, content)
            if span is None:
                continue
            start, end, label = span
            target = None
            username = None
            display_name = None
            is_bot = False
            if kind == "mention":
                username = label.removeprefix("@")
                is_bot = bool(
                    self._bot_username and label.casefold() == f"@{self._bot_username}".casefold()
                )
            else:
                target_user = getattr(entity, "user", None)
                if target_user is None:
                    continue
                target = UserRef(NAME, str(target_user.id))
                target_user_snapshot = self._user(target_user)
                username = target_user_snapshot.username
                display_name = target_user_snapshot.display_name
                is_bot = self._bot_id is not None and target_user.id == self._bot_id
            mentions_bot = mentions_bot or is_bot
            if len(mentions) < 16 and len(label) <= 128:
                mentions.append(
                    Mention(
                        start,
                        end,
                        kind,
                        label,
                        target=target,
                        username=username[:64] if username else None,
                        display_name=display_name[:128] if display_name else None,
                        is_bot=is_bot,
                    )
                )
        return tuple(mentions), mentions_bot

    @staticmethod
    def _attachments(message: Any) -> list[Attachment]:
        found: list[Attachment] = []
        if message.photo:
            photo = max(
                message.photo,
                key=lambda item: (item.file_size or 0, item.width * item.height),
            )
            found.append(
                Attachment(photo.file_id, AttachmentKind.PHOTO, size_bytes=photo.file_size)
            )
        for field, kind in (
            ("document", AttachmentKind.DOCUMENT),
            ("audio", AttachmentKind.AUDIO),
            ("video", AttachmentKind.VIDEO),
            ("voice", AttachmentKind.AUDIO),
        ):
            item = getattr(message, field, None)
            if item:
                found.append(
                    Attachment(
                        item.file_id,
                        kind,
                        filename=getattr(item, "file_name", None),
                        mime_type=getattr(item, "mime_type", None),
                        size_bytes=item.file_size,
                    )
                )
        return found

    @staticmethod
    def _user(tg_user: Any) -> User:
        return User(
            UserRef(NAME, str(tg_user.id)),
            display_name=" ".join(part for part in (tg_user.first_name, tg_user.last_name) if part),
            username=tg_user.username,
            is_bot=tg_user.is_bot,
        )

    @staticmethod
    def _message_ref(message: Any) -> MessageRef:
        thread_id = (
            str(message.message_thread_id) if getattr(message, "is_topic_message", False) else None
        )
        return MessageRef(ChatRef(NAME, str(message.chat.id)), str(message.message_id), thread_id)

    @staticmethod
    def _is_member_status(status: str, restricted_member: bool | None) -> bool:
        return status in ("creator", "administrator", "member") or (
            status == "restricted" and restricted_member is True
        )

    async def send(
        self,
        chat: ChatRef,
        text: str,
        *,
        reply_to: MessageRef | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        return await self._send_text(chat, text, reply_to=reply_to, thread_id=thread_id)

    async def send_reply_prompt(
        self,
        chat: ChatRef,
        text: str,
        *,
        reply_to: MessageRef | None = None,
        thread_id: str | None = None,
        placeholder: str = "",
    ) -> MessageRef:
        """Send ``text`` with a selective ``ForceReply`` on the last part, so the reply box opens
        for the person whose message it answers (``chatcore.command_prompts``)."""
        markup = ForceReply(selective=True, input_field_placeholder=placeholder[:64] or None)
        return await self._send_text(
            chat, text, reply_to=reply_to, thread_id=thread_id, last_markup=markup
        )

    async def _send_text(
        self,
        chat: ChatRef,
        text: str,
        *,
        reply_to: MessageRef | None = None,
        thread_id: str | None = None,
        last_markup: ForceReply | None = None,
    ) -> MessageRef:
        if not text:
            raise ValueError("message text cannot be empty")
        chunks = split_message(render_markdown(text), self.capabilities.max_message_len)
        sent = []
        for index, chunk in enumerate(chunks):
            args: dict[str, Any] = {
                "chat_id": chat.id,
                "text": chunk.text,
                "link_preview_options": LinkPreviewOptions(is_disabled=True),
            }
            if chunk.entities:
                args["entities"] = chunk.entities
            if index == 0 and reply_to:
                args["reply_parameters"] = ReplyParameters(message_id=int(reply_to.message_id))
            if thread_id:
                args["message_thread_id"] = int(thread_id)
            if last_markup is not None and index == len(chunks) - 1:
                args["reply_markup"] = last_markup
            sent.append(await self._send_with_retry(self._application.bot.send_message, **args))
        assert sent
        ref = MessageRef(chat, str(sent[-1].message_id), thread_id)
        self._sent_parts[(chat.key, ref.message_id)] = tuple(str(item.message_id) for item in sent)
        while len(self._sent_parts) > 1000:
            self._sent_parts.pop(next(iter(self._sent_parts)))
        return ref

    def sent_message_ids(self, ref: MessageRef) -> tuple[str, ...]:
        return self._sent_parts.get((ref.chat.key, ref.message_id), (ref.message_id,))

    async def _send_with_retry(self, call: Callable[..., Awaitable[Any]], **args: Any) -> Any:
        error_name: str | None = None
        try:
            return await call(**args)
        except RetryAfter as exc:
            delay = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else exc.retry_after
            )
            await asyncio.sleep(delay)
            try:
                return await call(**args)
            except TelegramError as retry_exc:
                error_name = type(retry_exc).__name__
        except TelegramError as exc:
            error_name = type(exc).__name__
        raise PlatformError(error_name or "TelegramError")

    async def send_poll(
        self,
        chat: ChatRef,
        question: str,
        options: Sequence[str],
        *,
        multiple: bool,
        anonymous: bool,
        thread_id: str | None = None,
    ) -> SentPoll:
        msg = await self._send_with_retry(
            self._application.bot.send_poll,
            chat_id=chat.id,
            question=question,
            options=list(options),
            is_anonymous=anonymous,
            allows_multiple_answers=multiple,
            type="regular",
            message_thread_id=int(thread_id) if thread_id else None,
        )
        return SentPoll(MessageRef(chat, str(msg.message_id), thread_id), msg.poll.id)

    async def send_file(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        if len(data) > MAX_FILE_BYTES:
            raise PlatformError("FileTooLarge")
        msg = await self._send_with_retry(
            self._application.bot.send_document,
            chat_id=chat.id,
            document=data,
            filename=filename,
            caption=caption,
            message_thread_id=int(thread_id) if thread_id else None,
        )
        return MessageRef(chat, str(msg.message_id), thread_id)

    async def send_voice(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        if (
            not isinstance(filename, str)
            or not _VOICE_FILENAME.fullmatch(filename)
            or not isinstance(data, bytes)
            or not data
        ):
            raise PlatformError("InvalidVoice")
        if len(data) > MAX_VOICE_BYTES:
            raise PlatformError("VoiceTooLarge")
        valid_id3 = data.startswith(b"ID3") and len(data) > 10
        valid_frame = (
            len(data) >= 4
            and data[0] == 0xFF
            and (data[1] & 0xE0) == 0xE0
            and (data[1] & 0x06) != 0
        )
        if not (valid_id3 or valid_frame):
            raise PlatformError("InvalidVoice")
        if caption is not None:
            from chatcore_telegram.formatting import utf16_length

            if utf16_length(caption) > 1024:
                raise PlatformError("CaptionTooLong")
        try:
            msg = await self._application.bot.send_voice(
                chat_id=chat.id,
                voice=InputFile(data, filename=filename),
                caption=caption,
                message_thread_id=int(thread_id) if thread_id else None,
            )
        except TelegramError as exc:
            logger.warning("telegram voice delivery failed reason=%s", _voice_error_category(exc))
            raise PlatformError(type(exc).__name__) from None
        return MessageRef(chat, str(msg.message_id), thread_id)

    async def send_photo(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        msg = await self._send_photo_message(
            chat, filename, data, caption=caption, thread_id=thread_id
        )
        return MessageRef(chat, str(msg.message_id), thread_id)

    async def send_photo_with_receipt(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> PhotoReceipt:
        msg = await self._send_photo_message(
            chat, filename, data, caption=caption, thread_id=thread_id
        )
        attachments = self._attachments(msg) if getattr(msg, "photo", None) else []
        photo = next((item for item in attachments if item.kind == AttachmentKind.PHOTO), None)
        return PhotoReceipt(MessageRef(chat, str(msg.message_id), thread_id), photo)

    async def _send_photo_message(
        self,
        chat: ChatRef,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> Any:
        if (
            not isinstance(filename, str)
            or not _PHOTO_FILENAME.fullmatch(filename)
            or not isinstance(data, bytes)
            or not data
            or not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"))
        ):
            raise PlatformError("InvalidPhoto")
        if len(data) > MAX_PHOTO_BYTES:
            raise PlatformError("PhotoTooLarge")
        dimensions = _image_size(data)
        if dimensions is None or sum(dimensions) > 10_000 or max(dimensions) / min(dimensions) > 20:
            raise PlatformError("InvalidPhoto")
        if caption is not None and utf16_length(caption) > 1024:
            raise PlatformError("CaptionTooLong")
        msg = await self._send_with_retry(
            self._application.bot.send_photo,
            chat_id=chat.id,
            photo=data,
            filename=filename,
            caption=caption,
            message_thread_id=int(thread_id) if thread_id else None,
        )
        return msg

    async def send_photos(
        self,
        chat: ChatRef,
        photos: Sequence[tuple[str, bytes]],
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRef:
        if not 2 <= len(photos) <= 10:
            raise PlatformError("InvalidPhoto")
        media = []
        for index, (filename, data) in enumerate(photos):
            if (
                not isinstance(filename, str)
                or not _PHOTO_FILENAME.fullmatch(filename)
                or not isinstance(data, bytes)
                or not data
                or not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"))
            ):
                raise PlatformError("InvalidPhoto")
            if len(data) > MAX_PHOTO_BYTES:
                raise PlatformError("PhotoTooLarge")
            size = _image_size(data)
            if size is None or sum(size) > 10_000 or max(size) / min(size) > 20:
                raise PlatformError("InvalidPhoto")
            if index == 0 and caption is not None and utf16_length(caption) > 1024:
                raise PlatformError("CaptionTooLong")
            media.append(
                InputMediaPhoto(data, filename=filename, caption=caption if index == 0 else None)
            )
        messages = await self._send_with_retry(
            self._application.bot.send_media_group,
            chat_id=chat.id,
            media=media,
            message_thread_id=int(thread_id) if thread_id else None,
        )
        return MessageRef(chat, str(messages[0].message_id), thread_id)

    async def stop_poll(self, message: MessageRef) -> PollResult:
        poll = await self._safe(
            self._application.bot.stop_poll,
            chat_id=message.chat.id,
            message_id=int(message.message_id),
        )
        return PollResult(
            tuple(option.voter_count for option in poll.options), poll.total_voter_count
        )

    async def edit(self, message: MessageRef, text: str) -> None:
        rendered = render_markdown(text)
        if utf16_length(rendered.text) > self.capabilities.max_message_len:
            raise ValueError("edited message exceeds Telegram's message limit")
        error_name: str | None = None
        try:
            await self._application.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=int(message.message_id),
                text=rendered.text,
                entities=rendered.entities,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).casefold():
                error_name = type(exc).__name__
        except TelegramError as exc:
            error_name = type(exc).__name__
        if error_name:
            raise PlatformError(error_name)

    async def typing(self, chat: ChatRef, thread_id: str | None = None) -> None:
        args: dict[str, Any] = {"chat_id": chat.id, "action": ChatAction.TYPING}
        if thread_id:
            args["message_thread_id"] = int(thread_id)
        await self._safe(self._application.bot.send_chat_action, **args)

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
        if not prompt:
            raise ValueError("confirmation prompt cannot be empty")
        confirm_data, cancel_data = f"c:{action_id}", f"x:{action_id}"
        if len(confirm_data.encode("utf-8")) > 64 or len(cancel_data.encode("utf-8")) > 64:
            raise ValueError("callback data must not exceed 64 UTF-8 bytes")
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Confirm", callback_data=confirm_data),
                    InlineKeyboardButton("Cancel", callback_data=cancel_data),
                ]
            ]
        )
        args: dict[str, Any] = {
            "chat_id": chat.id,
            "text": prompt,
            "reply_markup": markup,
            "link_preview_options": LinkPreviewOptions(is_disabled=True),
        }
        if reply_to:
            args["reply_parameters"] = ReplyParameters(message_id=int(reply_to.message_id))
        if thread_id:
            args["message_thread_id"] = int(thread_id)
        result = await self._send_with_retry(self._application.bot.send_message, **args)
        return MessageRef(chat, str(result.message_id), thread_id)

    async def download(self, attachment: Attachment, *, chat: ChatRef, max_bytes: int) -> bytes:
        limit = min(max(max_bytes, 0), MAX_FILE_BYTES)
        if attachment.size_bytes is not None and attachment.size_bytes > limit:
            raise PlatformError("FileTooLarge")
        deadline = asyncio.timeout(self._settings.download_timeout_s)
        try:
            async with deadline:
                return await self._download_with_retry(attachment, limit=limit)
        except TimeoutError:
            if deadline.expired():
                raise PlatformError("TimedOut") from None
            raise

    async def _download_with_retry(self, attachment: Attachment, *, limit: int) -> bytes:
        slept = 0.0
        retries = self._settings.download_retries
        timeout = self._settings.download_timeout_s
        for attempt in range(retries + 1):
            try:
                file = await self._application.bot.get_file(
                    attachment.id, connect_timeout=timeout, read_timeout=timeout
                )
                if file.file_size is not None and file.file_size > limit:
                    raise PlatformError("FileTooLarge")
                content = bytes(
                    await file.download_as_bytearray(connect_timeout=timeout, read_timeout=timeout)
                )
                if len(content) > limit:
                    raise PlatformError("FileTooLarge")
                return content
            except RetryAfter as exc:
                retry_after = exc.retry_after
                seconds = (
                    retry_after.total_seconds()
                    if hasattr(retry_after, "total_seconds")
                    else retry_after
                )
                if attempt >= retries or seconds > 8 - slept:
                    raise PlatformError(type(exc).__name__) from None
                logger.warning(
                    "telegram download retry attempt=%s error=%s",
                    attempt + 1,
                    error_label(exc),
                )
                await asyncio.sleep(seconds)
                slept += seconds
            except (TimedOut, NetworkError) as exc:
                if attempt >= retries or isinstance(exc, (BadRequest, Forbidden, InvalidToken)):
                    raise PlatformError(type(exc).__name__) from None
                delay = self._settings.download_backoff_s * (2**attempt)
                delay += random.uniform(0, delay * 0.1)  # noqa: S311 - non-security jitter
                delay = min(delay, max(0.0, 8.0 - slept))
                logger.warning(
                    "telegram download retry attempt=%s error=%s",
                    attempt + 1,
                    error_label(exc),
                )
                await asyncio.sleep(delay)
                slept += delay
            except PlatformError:
                raise
            except TelegramError as exc:
                raise PlatformError(type(exc).__name__) from None

    async def is_group_admin(self, chat: ChatRef, user: UserRef) -> bool:
        """Resolve current Telegram creator/admin status without consulting cached membership."""
        if chat.platform != NAME or user.platform != NAME:
            return False
        try:
            member = await self._application.bot.get_chat_member(chat.id, int(user.id))
        except Exception as exc:
            raise PlatformError(type(exc).__name__) from None
        return member.status in ("creator", "administrator")

    async def is_member(self, chat: ChatRef, user: UserRef) -> bool:
        error_name: str | None = None
        try:
            member = await self._application.bot.get_chat_member(chat.id, int(user.id))
        except BadRequest as exc:
            if "user not found" in str(exc).casefold():
                return False
            error_name = type(exc).__name__
        except TelegramError as exc:
            error_name = type(exc).__name__
        if error_name:
            raise PlatformError(error_name)
        return member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and member.is_member
        )

    async def leave(self, chat: ChatRef) -> None:
        await self._safe(self._application.bot.leave_chat, chat.id)

    async def pin(self, message: MessageRef) -> None:
        try:
            await self._application.bot.pin_chat_message(
                chat_id=message.chat.id,
                message_id=int(message.message_id),
                disable_notification=True,
            )
        except Exception as exc:
            logger.warning(
                "Telegram pin failed; bot needs the pin messages admin right: %s",
                type(exc).__name__,
            )
            raise PlatformError(type(exc).__name__) from None

    @classmethod
    async def _safe(cls, call: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        error_name: str | None = None
        try:
            return await call(*args, **kwargs)
        except TelegramError as exc:
            error_name = type(exc).__name__
        raise PlatformError(error_name or "TelegramError")


def _voice_error_category(exc: TelegramError) -> str:
    """Return a bounded diagnostic category without retaining Telegram's response text."""
    description = str(exc).casefold()
    if isinstance(exc, BadRequest):
        if "voice" in description or "audio" in description or "file" in description:
            return "media_rejected"
        if "caption" in description or "entity" in description:
            return "caption_rejected"
        if "thread" in description or "topic" in description:
            return "topic_rejected"
        if "chat" in description:
            return "destination_rejected"
        return "request_rejected"
    if isinstance(exc, Forbidden):
        return "forbidden"
    if isinstance(exc, RetryAfter):
        return "rate_limited"
    if isinstance(exc, (NetworkError, TimedOut)):
        return "network_error"
    return "telegram_error"


# Imported last: bridge.py depends only on chatcore and chatcore_telegram.formatting.
from chatcore_telegram.bridge import (  # noqa: E402
    BridgeDownloadSettings,
    BridgeTelegramPlatform,
    TelegramBridgeHTTPClient,
)

__all__ = [
    "BridgeDownloadSettings",
    "BridgeTelegramPlatform",
    "TelegramBridgeHTTPClient",
    "MAX_PHOTO_BYTES",
    "TelegramPlatform",
    "TelegramSettings",
]
