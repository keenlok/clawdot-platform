from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from chatcore.events import (
    Attachment,
    BotAddedToChat,
    ButtonPress,
    ChatRef,
    IncomingMessage,
    MemberJoined,
    MemberLeft,
    Mention,
    MessageRef,
    UserRef,
)
from chatcore.files import VoicePlatform
from chatcore.platform import Capabilities, ChatPlatform, PlatformError
from chatcore_telegram import TelegramPlatform, TelegramSettings
from pydantic import SecretStr
from telegram import (
    Audio,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    Chat,
    Document,
    Message,
    PhotoSize,
    Update,
    User,
    Video,
    Voice,
)
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import TypeHandler
from telegram.request import BaseRequest


class FakeRequest(BaseRequest):
    """Return canned Bot API data while recording URL paths and serialized payloads."""

    def __init__(self, *, token: str = "fake-token", fail_send: bool = False) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.token = token
        self.fail_send = fail_send

    @property
    def read_timeout(self) -> float:
        return 5

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_request(self, url: str, method: str, request_data=None, **kwargs):
        self.calls.append((url.rsplit("/", 1)[-1], method, request_data.json_parameters or {}))
        if url.endswith("/getMe"):
            response = {"id": 99, "is_bot": True, "first_name": "Bot", "username": "bot"}
        elif url.endswith("/sendMessage") and self.fail_send:
            return 400, json.dumps(
                {
                    "ok": False,
                    "error_code": 400,
                    "description": f"Bad Request: chat not found {self.token}",
                }
            ).encode()
        elif url.endswith("/getFile"):
            response = {
                "file_id": "file-id",
                "file_unique_id": "unique-id",
                "file_size": 4,
                "file_path": f"files/{self.token}/private.dat",
            }
        elif "/file/" in url:
            return 400, json.dumps(
                {
                    "ok": False,
                    "error_code": 400,
                    "description": f"download failed {self.token}",
                }
            ).encode()
        else:
            response = {
                "message_id": 17,
                "date": 1_767_225_600,
                "chat": {"id": -100, "type": "supergroup"},
                "text": "ok",
            }
        return 200, json.dumps({"ok": True, "result": response}).encode()


def make_platform(bot: object | None = None) -> TelegramPlatform:
    platform = TelegramPlatform(TelegramSettings(bot_token=SecretStr("fake-token")))
    platform._application = SimpleNamespace(bot=bot or AsyncMock())
    return platform


def user(ident: int = 7, *, is_bot: bool = False) -> User:
    return User(ident, "Ada", is_bot=is_bot, username="ada")


def msg(**kwargs: object) -> Message:
    base = {
        "message_id": 10,
        "date": datetime(2026, 1, 1, tzinfo=UTC),
        "chat": Chat(-100, "supergroup"),
        "from_user": user(),
    }
    base.update(kwargs)
    return Message(**base)


async def test_ptb_dispatch_passes_update_and_context_to_adapter() -> None:
    platform = TelegramPlatform(
        TelegramSettings(bot_token=SecretStr("fake-token")), request=FakeRequest()
    )
    app = platform._application
    app.add_handler(TypeHandler(Update, platform._handle_update))
    platform._handler = AsyncMock()
    await app.initialize()
    try:
        await app.process_update(Update(1, message=msg(chat=Chat(7, "private"), text="/help")))
        platform._handler.assert_awaited_once()
        event = platform._handler.call_args.args[0]
        assert isinstance(event, IncomingMessage)
        assert event.chat == ChatRef("tg", "7")
        assert event.text == "/help"
    finally:
        await app.shutdown()


async def test_send_voice_uploads_mp3_to_exact_chat_topic_and_caption() -> None:
    bot = AsyncMock()
    bot.send_voice.return_value = SimpleNamespace(message_id=29)
    platform = make_platform(bot)
    chat = ChatRef("tg", "-100")
    assert isinstance(platform, VoicePlatform)
    result = await platform.send_voice(
        chat,
        "pronunciation.mp3",
        b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64",
        caption="こんにちは\nNormal",
        thread_id="42",
    )
    assert result == MessageRef(chat, "29", "42")
    bot.send_voice.assert_awaited_once_with(
        chat_id="-100",
        voice=ANY,
        caption="こんにちは\nNormal",
        message_thread_id=42,
    )
    assert bot.send_voice.await_args.kwargs["voice"].mimetype == "audio/mpeg"
    assert bot.send_voice.await_args.kwargs["voice"].filename == "pronunciation.mp3"


async def test_send_voice_accepts_other_plain_mp3_names_up_to_eight_mib() -> None:
    bot = AsyncMock()
    bot.send_voice.return_value = SimpleNamespace(message_id=30)
    platform = make_platform(bot)
    chat = ChatRef("tg", "-100")
    header = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"
    large = header + b"\x00" * (8 * 1024 * 1024 - len(header))
    await platform.send_voice(chat, "narration.mp3", large, caption="🔊 News")
    assert bot.send_voice.await_args.kwargs["voice"].filename == "narration.mp3"


async def test_voice_rejects_bounds_and_sanitizes_one_ambiguous_send() -> None:
    from telegram.error import BadRequest, NetworkError

    bot = AsyncMock()
    platform = make_platform(bot)
    chat = ChatRef("tg", "-100")
    valid = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64"
    for name in (
        "../unsafe.mp3",
        "voice.ogg",
        "Narration.mp3",
        "a b.mp3",
        "x" * 65 + ".mp3",
        ".mp3",
    ):
        with pytest.raises(PlatformError):
            await platform.send_voice(chat, name, valid)
    with pytest.raises(PlatformError):
        await platform.send_voice(chat, "pronunciation.mp3", b"invalid")
    with pytest.raises(PlatformError):
        await platform.send_voice(chat, "pronunciation.mp3", valid + b"x" * (8 * 1024 * 1024))
    with pytest.raises(PlatformError):
        await platform.send_voice(chat, "pronunciation.mp3", valid, caption="x" * 1025)
    assert bot.send_voice.await_count == 0

    bot.send_voice.side_effect = BadRequest("provider body and credential detail")
    with pytest.raises(PlatformError) as error:
        await platform.send_voice(chat, "pronunciation.mp3", valid)
    assert "credential detail" not in str(error.value)
    assert bot.send_voice.await_count == 1

    bot.send_voice.side_effect = NetworkError("ambiguous secret detail")
    with pytest.raises(PlatformError) as error:
        await platform.send_voice(chat, "pronunciation.mp3", valid)
    assert "ambiguous secret detail" not in str(error.value)
    assert bot.send_voice.await_count == 2


async def test_voice_send_logs_only_allowlisted_rejection_category(caplog) -> None:
    bot = AsyncMock()
    platform = make_platform(bot)
    chat = ChatRef("tg", "-100")
    bot.send_voice.side_effect = BadRequest(
        "Bad Request: voice file must be audio/ogg; private caption text"
    )

    with caplog.at_level(logging.WARNING, logger="chatcore_telegram"):
        with pytest.raises(PlatformError):
            await platform.send_voice(
                chat,
                "pronunciation.mp3",
                b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64",
                caption="private phrase",
            )

    assert "telegram voice delivery failed reason=media_rejected" in caplog.text
    assert "private caption text" not in caplog.text
    assert "private phrase" not in caplog.text


@pytest.mark.parametrize("private", [False, True])
async def test_current_group_title_maps_as_optional_chat_context(private) -> None:
    platform = make_platform()
    platform._bot_id = 99
    chat = Chat(7 if private else -100, "private" if private else "supergroup", title="SexBomb")
    event = await platform._map_message(msg(chat=chat, text="hi"))
    assert event.chat_title == (None if private else "SexBomb")
    assert event.chat == ChatRef("tg", str(chat.id))
    assert event.chat.id != "*"
    assert event.user.ref.id != "*"


@pytest.mark.parametrize("kind", ["group", "dm", "topic", "reply", "mention", "files"])
async def test_message_mapping(kind: str) -> None:
    platform = make_platform()
    bot_user = user(99, is_bot=True)
    platform._application.bot.get_me = AsyncMock(return_value=bot_user)
    chat = Chat(7, "private") if kind == "dm" else Chat(-100, "supergroup")
    values: dict[str, object] = {"chat": chat}
    if kind == "topic":
        values.update(message_thread_id=42, is_topic_message=True)
    if kind == "reply":
        values["reply_to_message"] = msg(from_user=bot_user)
    if kind == "mention":
        from telegram import MessageEntity

        values.update(text="hello @bot", entities=[MessageEntity("mention", 6, 4)])
        platform._application.bot.get_me = AsyncMock(
            return_value=User(99, "Bot", is_bot=True, username="bot")
        )
    if kind == "files":
        values.update(
            caption="files",
            photo=[PhotoSize("small", "u1", 1, 1, 10), PhotoSize("large", "u2", 2, 2, 20)],
            document=Document("doc", "ud", "name.txt", "text/plain", 20),
            audio=Audio("aud", "ua", 3, mime_type="audio/mpeg", file_size=30, file_name="a.mp3"),
            video=Video(
                "vid", "uv", 1, 1, 4, mime_type="video/mp4", file_size=40, file_name="v.mp4"
            ),
            voice=Voice("voc", "uvc", 5, "audio/ogg", 50),
        )
    event = await platform._map_message(msg(**({"text": "hello", **values})))
    assert isinstance(event, IncomingMessage)
    assert event.chat == ChatRef("tg", str(chat.id))
    assert event.is_dm is (kind == "dm")
    assert event.user.is_bot is False
    assert event.sent_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert event.thread_id == ("42" if kind == "topic" else None)
    assert event.reply_to_bot is (kind == "reply")
    assert (event.reply_to is not None) is (kind == "reply")
    assert event.mentions_bot is (kind == "mention")
    if kind == "files":
        assert [a.id for a in event.attachments] == ["large", "doc", "aud", "vid", "voc"]
        assert event.attachments[1].filename == "name.txt"
        assert event.attachments[1].mime_type == "text/plain"


async def test_member_events_and_bot_added() -> None:
    platform = make_platform()
    joined = user(12)
    chat = Chat(-100, "group")
    join_msg = msg(chat=chat, new_chat_members=[joined])
    leave_msg = msg(chat=chat, left_chat_member=joined)
    platform._handler = AsyncMock()
    await platform._handle_update(
        SimpleNamespace(
            message=join_msg,
            edited_message=None,
            channel_post=None,
            edited_channel_post=None,
            callback_query=None,
            chat_member=None,
            my_chat_member=None,
        )
    )
    await platform._handle_update(
        SimpleNamespace(
            message=leave_msg,
            edited_message=None,
            channel_post=None,
            edited_channel_post=None,
            callback_query=None,
            chat_member=None,
            my_chat_member=None,
        )
    )
    assert isinstance(platform._handler.call_args_list[0].args[0], MemberJoined)
    assert isinstance(platform._handler.call_args_list[1].args[0], MemberLeft)

    platform._application.bot.id = 99
    platform._bot_id = 99
    update = SimpleNamespace(
        my_chat_member=SimpleNamespace(
            chat=chat,
            from_user=joined,
            old_chat_member=SimpleNamespace(user=user(99, is_bot=True), status="left"),
            new_chat_member=SimpleNamespace(user=user(99, is_bot=True), status="member"),
        ),
        message=None,
        edited_message=None,
        channel_post=None,
        edited_channel_post=None,
        callback_query=None,
        chat_member=None,
    )
    events = []

    async def collect(event: object) -> None:
        events.append(event)

    platform._handler = collect
    await platform._handle_update(update)
    assert isinstance(events[0], BotAddedToChat)


async def test_chat_member_transition_with_restricted_member() -> None:
    platform = make_platform()
    events = []

    async def collect(event: object) -> None:
        events.append(event)

    platform._handler = collect
    member = user(12)
    update = SimpleNamespace(
        chat=Chat(-100, "group"),
        old_chat_member=SimpleNamespace(user=member, status="restricted", is_member=False),
        new_chat_member=SimpleNamespace(user=member, status="restricted", is_member=True),
    )
    await platform._handle_update(
        SimpleNamespace(
            chat_member=update,
            callback_query=None,
            message=None,
            edited_message=None,
            channel_post=None,
            edited_channel_post=None,
            my_chat_member=None,
        )
    )
    assert isinstance(events[0], MemberJoined)


@pytest.mark.parametrize(("data", "choice"), [("c:act", "confirm"), ("x:act", "cancel")])
async def test_callback_mapping_and_answer(data: str, choice: str) -> None:
    platform = make_platform()
    query = SimpleNamespace(
        data=data,
        from_user=user(),
        message=msg(),
        answer=AsyncMock(),
    )
    platform._handler = AsyncMock()
    await platform._handle_update(
        SimpleNamespace(
            callback_query=query,
            message=None,
            edited_message=None,
            channel_post=None,
            edited_channel_post=None,
            my_chat_member=None,
            chat_member=None,
        )
    )
    event = platform._handler.call_args.args[0]
    assert isinstance(event, ButtonPress) and event.choice == choice
    query.answer.assert_awaited_once()


async def test_malformed_callback_is_answered_and_ignored() -> None:
    platform = make_platform()
    query = SimpleNamespace(data="bad", from_user=user(), message=msg(), answer=AsyncMock())
    platform._handler = AsyncMock()
    await platform._handle_update(
        SimpleNamespace(
            callback_query=query,
            message=None,
            edited_message=None,
            channel_post=None,
            edited_channel_post=None,
            my_chat_member=None,
            chat_member=None,
        )
    )
    query.answer.assert_awaited_once()
    platform._handler.assert_not_awaited()


async def test_edited_messages_are_ignored() -> None:
    platform = make_platform()
    platform._handler = AsyncMock()
    await platform._handle_update(
        SimpleNamespace(
            callback_query=None,
            message=None,
            edited_message=msg(),
            channel_post=None,
            edited_channel_post=None,
            my_chat_member=None,
            chat_member=None,
        )
    )
    platform._handler.assert_not_awaited()


async def test_mention_entity_offsets_count_utf16_units() -> None:
    from telegram import MessageEntity

    platform = make_platform()
    platform._bot_id, platform._bot_username = 99, "bot"
    message = msg(text="💬 @bot", entities=[MessageEntity("mention", 3, 4)])
    event = await platform._map_message(message)
    assert event is not None and event.mentions_bot
    assert event.mentions == (Mention(2, 6, "mention", "@bot", username="bot", is_bot=True),)


async def test_text_mentions_keep_distinct_targets_and_python_offsets_after_emoji() -> None:
    from telegram import MessageEntity

    platform = make_platform()
    platform._bot_id, platform._bot_username = 99, "bot"
    text = "💬 Alex Alex"
    entities = [
        MessageEntity("text_mention", 3, 4, user=user(8)),
        MessageEntity("text_mention", 8, 4, user=user(9)),
    ]
    event = await platform._map_message(msg(text=text, entities=entities))

    assert event is not None and not event.mentions_bot
    assert [text[item.start : item.end] for item in event.mentions] == ["Alex", "Alex"]
    assert [item.target.id for item in event.mentions] == ["8", "9"]
    assert [item.start for item in event.mentions] == [2, 7]


async def test_caption_entities_and_bot_mention_past_metadata_cap() -> None:
    from telegram import MessageEntity

    platform = make_platform()
    platform._bot_id, platform._bot_username = 99, "bot"
    text = " ".join(["@friend"] * 17 + ["@bot"])
    entities = [MessageEntity("mention", offset, len(label)) for offset, label in []]
    offset = 0
    for label in ["@friend"] * 17 + ["@bot"]:
        entities.append(MessageEntity("mention", offset, len(label)))
        offset += len(label) + 1
    event = await platform._map_message(
        msg(caption=text, caption_entities=entities, photo=[PhotoSize("p", "u", 1, 1)])
    )

    assert event is not None and event.mentions_bot
    assert len(event.mentions) == 16
    assert all(item.text == "@friend" for item in event.mentions)


async def test_invalid_surrogate_split_mention_entity_is_ignored() -> None:
    from telegram import MessageEntity

    platform = make_platform()
    platform._bot_id, platform._bot_username = 99, "bot"
    event = await platform._map_message(msg(text="💬", entities=[MessageEntity("mention", 1, 1)]))

    assert event is not None and not event.mentions_bot
    assert event.mentions == ()


async def test_negative_and_out_of_bounds_entity_spans_are_ignored() -> None:
    platform = make_platform()
    platform._bot_id, platform._bot_username = 99, "bot"
    invalid = [
        SimpleNamespace(type="mention", offset=-1, length=4),
        SimpleNamespace(type="mention", offset=100, length=4),
    ]

    event = await platform._map_message(msg(text="@bot", entities=invalid))

    assert event is not None and not event.mentions_bot
    assert event.mentions == ()


async def test_text_mention_targets_the_verified_bot_by_user_id() -> None:
    from telegram import MessageEntity

    platform = make_platform()
    platform._bot_id, platform._bot_username = 99, "bot"
    event = await platform._map_message(
        msg(text="Bot, please", entities=[MessageEntity("text_mention", 0, 3, user=user(99))])
    )

    assert event is not None and event.mentions_bot
    assert len(event.mentions) == 1
    assert event.mentions[0].target == UserRef("tg", "99")
    assert event.mentions[0].is_bot


async def test_forum_topic_root_is_not_a_reply() -> None:
    platform = make_platform()
    platform._bot_id = 99
    topic_root = msg(from_user=user(99, is_bot=True), forum_topic_created=object())
    message = msg(
        text="topic message",
        is_topic_message=True,
        message_thread_id=42,
        reply_to_message=topic_root,
    )
    event = await platform._map_message(message)
    assert event is not None
    assert event.reply_to is None and event.reply_to_bot is False


async def test_real_bot_reply_inside_topic_is_preserved() -> None:
    platform = make_platform()
    platform._bot_id = 99
    bot_message = msg(from_user=user(99, is_bot=True), is_topic_message=True, message_thread_id=42)
    message = msg(
        text="reply",
        is_topic_message=True,
        message_thread_id=42,
        reply_to_message=bot_message,
    )
    event = await platform._map_message(message)
    assert event is not None and event.reply_to_bot
    assert event.reply_to is not None and event.reply_to.thread_id == "42"


async def test_send_splits_disables_previews_and_returns_last_ref() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = [SimpleNamespace(message_id=i) for i in (1, 2)]
    platform = make_platform(bot)
    ref = await platform.send(ChatRef("tg", "-100"), "a" * 4095 + "\nb")
    assert ref.message_id == "2"
    assert [len(call.kwargs["text"]) for call in bot.send_message.await_args_list] == [4096, 1]
    assert all(
        call.kwargs["link_preview_options"].is_disabled for call in bot.send_message.await_args_list
    )


async def test_sent_message_ids_tracks_split_parts_and_falls_back_for_unknown_ref() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = [SimpleNamespace(message_id=i) for i in (1, 2, 3)]
    platform = make_platform(bot)
    chat = ChatRef("tg", "-100")
    ref = await platform.send(chat, "a" * 8193)

    assert platform.sent_message_ids(ref) == ("1", "2", "3")
    unknown = MessageRef(chat, "900")
    assert platform.sent_message_ids(unknown) == ("900",)


async def test_confirm_keyboard_and_edit_preview_disabled() -> None:
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=11)
    platform = make_platform(bot)
    ref = await platform.confirm(ChatRef("tg", "-100"), "Proceed?", "abc", UserRef("tg", "7"))
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert [[button.callback_data for button in row] for row in markup.inline_keyboard] == [
        ["c:abc", "x:abc"]
    ]
    assert bot.send_message.await_args.kwargs["link_preview_options"].is_disabled
    bot.edit_message_text.side_effect = BadRequest("fake token text")
    with pytest.raises(PlatformError) as error:
        await platform.edit(ref, "edited")
    assert "fake token text" not in str(error.value)
    assert bot.edit_message_text.await_args.kwargs["link_preview_options"].is_disabled


async def test_callback_data_over_64_bytes_is_rejected() -> None:
    bot = AsyncMock()
    platform = make_platform(bot)
    with pytest.raises(ValueError):
        await platform.confirm(ChatRef("tg", "-100"), "prompt", "a" * 63, UserRef("tg", "7"))
    bot.send_message.assert_not_awaited()


async def test_empty_send_and_confirm_are_rejected() -> None:
    bot = AsyncMock()
    platform = make_platform(bot)
    with pytest.raises(ValueError):
        await platform.send(ChatRef("tg", "-100"), "")
    with pytest.raises(ValueError):
        await platform.confirm(ChatRef("tg", "-100"), "", "action", UserRef("tg", "7"))
    bot.send_message.assert_not_awaited()


async def test_download_refuses_size_before_fetch() -> None:
    bot = AsyncMock()
    bot.get_file.return_value = SimpleNamespace(file_size=12, download_as_bytearray=AsyncMock())
    platform = make_platform(bot)
    with pytest.raises(PlatformError):
        await platform.download(
            Attachment("f", "document"),
            chat=ChatRef("tg", "-100"),
            max_bytes=10,
        )
    bot.get_file.assert_awaited_once()
    bot.get_file.return_value.download_as_bytearray.assert_not_awaited()


async def test_download_refuses_known_attachment_size_without_fetch() -> None:
    bot = AsyncMock()
    platform = make_platform(bot)
    with pytest.raises(PlatformError):
        await platform.download(
            Attachment("f", "document", size_bytes=11),
            chat=ChatRef("tg", "-100"),
            max_bytes=10,
        )
    bot.get_file.assert_not_awaited()


async def test_download_forwards_timeout_to_both_telegram_stages():
    bot = AsyncMock()
    file = SimpleNamespace(file_size=2, download_as_bytearray=AsyncMock(return_value=b"ok"))
    bot.get_file.return_value = file
    platform = make_platform(bot)

    assert (
        await platform.download(
            Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10
        )
        == b"ok"
    )

    bot.get_file.assert_awaited_once_with("f", connect_timeout=30.0, read_timeout=30.0)
    file.download_as_bytearray.assert_awaited_once_with(connect_timeout=30.0, read_timeout=30.0)


@pytest.mark.parametrize("stalled_stage", ["get_file", "download"])
async def test_download_total_deadline_cancels_stalled_stage_safely(stalled_stage):
    bot = AsyncMock()
    entered = asyncio.Event()

    async def stall(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    file = SimpleNamespace(file_size=2, download_as_bytearray=AsyncMock(side_effect=stall))
    if stalled_stage == "get_file":
        bot.get_file.side_effect = stall
    else:
        bot.get_file.return_value = file
    platform = make_platform(bot)
    platform._settings.download_timeout_s = 0.02

    with pytest.raises(PlatformError, match="^TimedOut$"):
        await asyncio.wait_for(
            platform.download(
                Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10
            ),
            timeout=1,
        )
    assert entered.is_set()


async def test_download_deadline_includes_retry_sleep(monkeypatch):
    bot = AsyncMock()
    bot.get_file.side_effect = TimedOut("private token-bearing details")
    sleep_started = asyncio.Event()

    async def stall_sleep(_delay):
        sleep_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("chatcore_telegram.asyncio.sleep", stall_sleep)
    platform = make_platform(bot)
    platform._settings.download_timeout_s = 0.02
    with pytest.raises(PlatformError, match="^TimedOut$") as error:
        await asyncio.wait_for(
            platform.download(
                Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10
            ),
            timeout=1,
        )
    assert sleep_started.is_set()
    assert bot.get_file.await_count == 1
    assert "private token-bearing details" not in str(error.value)


async def test_download_external_cancellation_propagates():
    bot = AsyncMock()
    entered = asyncio.Event()

    async def stall(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    bot.get_file.side_effect = stall
    platform = make_platform(bot)
    platform._settings.download_timeout_s = 30
    task = asyncio.create_task(
        platform.download(Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()


async def test_download_preserves_inner_timeout_error():
    bot = AsyncMock()
    bot.get_file.side_effect = TimeoutError("inner operation timeout")
    platform = make_platform(bot)
    with pytest.raises(TimeoutError, match="inner operation timeout"):
        await platform.download(
            Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10
        )


@pytest.mark.parametrize("timeout", [0, -0.1, 120.1])
def test_download_timeout_setting_must_be_within_bounds(timeout):
    with pytest.raises(ValueError):
        TelegramSettings(bot_token=SecretStr("fake-token"), download_timeout_s=timeout)


def test_download_timeout_setting_defaults_to_thirty_seconds():
    settings = TelegramSettings(bot_token=SecretStr("fake-token"))
    assert settings.download_timeout_s == 30


def test_download_timeout_setting_reads_telegram_environment_alias(monkeypatch):
    monkeypatch.setenv("TELEGRAM_DOWNLOAD_TIMEOUT_S", "42")
    settings = TelegramSettings(bot_token=SecretStr("fake-token"))
    assert settings.download_timeout_s == 42


@pytest.mark.parametrize("failure", [TimedOut("timeout"), NetworkError("offline")])
async def test_download_retries_temporary_transport_errors(monkeypatch, failure) -> None:
    bot = AsyncMock()
    file = SimpleNamespace(file_size=2, download_as_bytearray=AsyncMock(return_value=b"ok"))
    bot.get_file.side_effect = [failure, file]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("chatcore_telegram.asyncio.sleep", sleep)
    monkeypatch.setattr("chatcore_telegram.random.uniform", lambda *_: 0)
    platform = make_platform(bot)
    assert (
        await platform.download(
            Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10
        )
        == b"ok"
    )
    assert bot.get_file.await_count == 2 and sleeps == [0.5]


async def test_download_uses_configured_exponential_backoff(monkeypatch) -> None:
    bot = AsyncMock()
    file = SimpleNamespace(file_size=2, download_as_bytearray=AsyncMock(return_value=b"ok"))
    bot.get_file.side_effect = [TimedOut("one"), NetworkError("two"), file]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("chatcore_telegram.asyncio.sleep", sleep)
    monkeypatch.setattr("chatcore_telegram.random.uniform", lambda *_: 0)
    platform = TelegramPlatform(
        TelegramSettings(bot_token=SecretStr("fake-token"), download_retries=2),
        request=FakeRequest(),
    )
    platform._application.bot = bot
    assert (
        await platform.download(
            Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10
        )
        == b"ok"
    )
    assert bot.get_file.await_count == 3
    assert sleeps == [0.5, 1.0]


async def test_download_retry_after_limit_and_bad_request(monkeypatch) -> None:
    bot = AsyncMock()
    file = SimpleNamespace(file_size=2, download_as_bytearray=AsyncMock(return_value=b"ok"))
    bot.get_file.side_effect = [RetryAfter(2), file]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("chatcore_telegram.asyncio.sleep", sleep)
    platform = make_platform(bot)
    assert (
        await platform.download(
            Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10
        )
        == b"ok"
    )
    assert sleeps == [2]
    bot.get_file.reset_mock()
    bot.get_file.side_effect = RetryAfter(30)
    with pytest.raises(PlatformError, match="RetryAfter"):
        await platform.download(
            Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10
        )
    assert bot.get_file.await_count == 1
    bot.get_file.reset_mock()
    bot.get_file.side_effect = BadRequest("invalid")
    with pytest.raises(PlatformError, match="BadRequest"):
        await platform.download(
            Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10
        )
    assert bot.get_file.await_count == 1


@pytest.mark.parametrize(
    ("status", "member", "expected"),
    [
        ("creator", None, True),
        ("administrator", None, True),
        ("member", None, True),
        ("restricted", True, True),
        ("restricted", False, False),
        ("left", None, False),
    ],
)
async def test_is_member_status_mapping(status: str, member: bool | None, expected: bool) -> None:
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(status=status, is_member=member)
    assert await make_platform(bot).is_member(ChatRef("tg", "-100"), UserRef("tg", "7")) is expected


async def test_retry_after_once_and_token_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = [RetryAfter(0), BadRequest("fake-token secret URL")]
    platform = make_platform(bot)
    with pytest.raises(PlatformError) as error:
        await platform.send(ChatRef("tg", "-100"), "hello")
    assert "fake-token" not in str(error.value)
    assert "secret URL" not in caplog.text
    assert bot.send_message.await_count == 2


async def test_conformance_settings_and_capabilities() -> None:
    platform = make_platform()
    assert isinstance(platform, ChatPlatform)
    assert platform.name == "tg"
    assert platform.capabilities == Capabilities(
        True, True, True, True, True, 4096, "markdown", True
    )
    assert isinstance(TelegramSettings(bot_token=SecretStr("x")).bot_token, SecretStr)


async def test_p_pin_calls_api_without_notification() -> None:
    request = FakeRequest()
    platform = TelegramPlatform(
        TelegramSettings(bot_token=SecretStr("fake-token")), request=request
    )
    await platform._application.bot.initialize()
    assert platform.capabilities.pin is True
    await platform.pin(MessageRef(ChatRef("tg", "-100123"), "42"))
    method, http_method, payload = request.calls[-1]
    assert method == "pinChatMessage" and http_method == "POST"
    assert payload["chat_id"] == "-100123"
    assert payload["message_id"] == "42"
    assert payload["disable_notification"] == "true"


async def test_p_pin_failure_raises_platform_error() -> None:
    bot = AsyncMock()
    bot.pin_chat_message.side_effect = ValueError("private")
    platform = make_platform(bot)
    with pytest.raises(PlatformError, match="ValueError"):
        await platform.pin(MessageRef(ChatRef("tg", "1"), "2"))


def test_token_loads_from_required_environment_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-env-token")
    assert TelegramSettings().bot_token.get_secret_value() == "fake-env-token"


async def test_de_json_updates_and_bot_api_use_fake_request() -> None:
    request = FakeRequest()
    platform = TelegramPlatform(
        TelegramSettings(bot_token=SecretStr("fake-token")), request=request
    )
    update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 8,
                "date": 1_767_225_600,
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 7, "is_bot": False, "first_name": "Ada"},
                "text": "hello @bot",
                "entities": [{"type": "mention", "offset": 6, "length": 4}],
            },
        },
        platform._application.bot,
    )
    platform._bot_id, platform._bot_username = 99, "bot"
    platform._handler = AsyncMock()
    await platform._handle_update(update)
    assert platform._handler.call_args.args[0].mentions_bot

    await platform._application.bot.initialize()
    await platform.send(ChatRef("tg", "-100"), "hello")
    method, http_method, payload = request.calls[-1]
    assert method == "sendMessage" and http_method == "POST"
    assert json.loads(payload["link_preview_options"]) == {"is_disabled": True}


async def test_application_processes_updates_concurrently() -> None:
    platform = TelegramPlatform(TelegramSettings(bot_token=SecretStr("fake-token")))
    assert platform._application.concurrent_updates > 1


async def test_native_formatting_is_serialized_to_bot_api() -> None:
    request = FakeRequest()
    platform = TelegramPlatform(
        TelegramSettings(bot_token=SecretStr("fake-token")), request=request
    )
    await platform._application.bot.initialize()
    try:
        ref = await platform.send(ChatRef("tg", "-100"), "😀 **bold** & `x`")
        method, _, payload = request.calls[-1]
        assert method == "sendMessage"
        assert payload["text"] == "😀 bold & x"
        assert json.loads(payload["entities"]) == [
            {"type": "bold", "offset": 3, "length": 4},
            {"type": "code", "offset": 10, "length": 1},
        ]
        assert "parse_mode" not in payload
        await platform.edit(ref, "**Edited**")
        method, _, payload = request.calls[-1]
        assert method == "editMessageText"
        assert payload["text"] == "Edited"
        assert json.loads(payload["entities"]) == [{"type": "bold", "offset": 0, "length": 6}]
    finally:
        await platform._application.bot.shutdown()


async def test_callback_answer_failure_still_dispatches_press(
    caplog: pytest.LogCaptureFixture,
) -> None:
    platform = make_platform()
    query = SimpleNamespace(
        data="c:act",
        from_user=user(),
        message=msg(),
        answer=AsyncMock(side_effect=BadRequest("stale token detail")),
    )
    platform._handler = AsyncMock()
    await platform._handle_update(
        SimpleNamespace(
            callback_query=query,
            message=None,
            edited_message=None,
            channel_post=None,
            edited_channel_post=None,
            my_chat_member=None,
            chat_member=None,
        )
    )
    event = platform._handler.call_args.args[0]
    assert isinstance(event, ButtonPress)
    platform._handler.assert_awaited_once_with(event)
    assert "stale token detail" not in caplog.text


async def test_handler_error_is_logged_without_details(caplog: pytest.LogCaptureFixture) -> None:
    platform = TelegramPlatform(TelegramSettings(bot_token=SecretStr("error-fake-token")))
    state = SimpleNamespace(poll_started=asyncio.Event(), error_handler=None)

    class App:
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=99, username="bot")))
        updater = SimpleNamespace(running=False)
        running = False

        def add_handler(self, handler: object) -> None:
            pass

        def add_error_handler(self, handler: object) -> None:
            state.error_handler = handler

        async def initialize(self) -> None:
            pass

        async def start(self) -> None:
            self.running = True

        async def shutdown(self) -> None:
            pass

        async def stop(self) -> None:
            self.running = False

    async def start_polling(**kwargs: object) -> None:
        assert "poll_answer" in kwargs["allowed_updates"]
        state.poll_started.set()

    async def stop_updater() -> None:
        pass

    App.updater.start_polling = start_polling
    App.updater.stop = stop_updater
    platform._application = App()
    task = asyncio.create_task(platform.start(AsyncMock()))
    await state.poll_started.wait()
    await platform.stop()
    await task

    error = BadRequest("error-fake-token secret message text")
    with caplog.at_level(logging.DEBUG):
        await state.error_handler(None, SimpleNamespace(error=error))
    assert "BadRequest" in caplog.text
    assert "error-fake-token" not in caplog.text
    assert "secret message text" not in caplog.text


@pytest.mark.parametrize("failure", ["get_me", "start"])
async def test_start_shuts_down_after_startup_failure(failure: str) -> None:
    platform = TelegramPlatform(TelegramSettings(bot_token=SecretStr("fake-token")))
    state = SimpleNamespace(shutdown=False)

    class App:
        updater = SimpleNamespace(running=False)
        running = False

        def __init__(self) -> None:
            self.bot = SimpleNamespace(get_me=self.get_me)

        def add_handler(self, handler: object) -> None:
            pass

        def add_error_handler(self, handler: object) -> None:
            pass

        async def initialize(self) -> None:
            pass

        async def get_me(self) -> object:
            if failure == "get_me":
                raise BadRequest("fake failure")
            return SimpleNamespace(id=99, username="bot")

        async def start(self) -> None:
            if failure == "start":
                raise BadRequest("fake failure")
            self.running = True

        async def shutdown(self) -> None:
            state.shutdown = True

        async def stop(self) -> None:
            self.running = False

    platform._application = App()
    with pytest.raises(PlatformError):
        await platform.start(AsyncMock())
    assert state.shutdown


async def test_start_sets_valid_menu_commands_and_ignores_menu_api_failure(caplog) -> None:
    platform = TelegramPlatform(TelegramSettings(bot_token=SecretStr("fake-token")))
    platform.set_menu_commands(
        [("reset", "x" * 300), ("Invalid", "skip"), ("bad-name", "skip")],
        [("compact", "Compact now")],
    )
    poll_started = asyncio.Event()
    set_commands = AsyncMock(side_effect=[BadRequest("private menu failure details"), None])

    class App:
        running = False
        updater = SimpleNamespace(running=False)

        def __init__(self):
            self.bot = SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(id=99, username="bot")),
                set_my_commands=set_commands,
            )

        def add_handler(self, handler):
            pass

        def add_error_handler(self, handler):
            pass

        async def initialize(self):
            pass

        async def start(self):
            self.running = True

        async def stop(self):
            self.running = False

        async def shutdown(self):
            pass

    async def start_polling(**_kwargs):
        poll_started.set()

    async def stop_updater():
        pass

    platform._application = App()
    platform._application.updater.start_polling = start_polling
    platform._application.updater.stop = stop_updater
    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(platform.start(AsyncMock()))
        await poll_started.wait()
        await platform.stop()
        await task

    assert set_commands.await_count == 2
    private_call, group_call = set_commands.await_args_list
    assert isinstance(private_call.kwargs["scope"], BotCommandScopeAllPrivateChats)
    assert isinstance(group_call.kwargs["scope"], BotCommandScopeAllGroupChats)
    assert len(private_call.args[0]) == 1
    assert private_call.args[0][0].command == "reset"
    assert len(private_call.args[0][0].description) == 256
    assert group_call.args[0][0].command == "compact"
    assert "private menu failure details" not in caplog.text
    assert "BadRequest" in caplog.text


async def test_real_ptb_send_and_download_errors_do_not_leak_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "123456:fake-test-token-not-real"
    request = FakeRequest(token=token, fail_send=True)
    platform = TelegramPlatform(TelegramSettings(bot_token=SecretStr(token)), request=request)
    await platform._application.bot.initialize()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PlatformError) as send_error:
            await platform.send(ChatRef("tg", "-100"), "hello")
    assert token not in caplog.text
    assert token not in str(send_error.value)
    assert token not in repr(send_error.value)
    assert token not in repr(send_error.value.__cause__)
    assert token not in repr(send_error.value.__context__)

    request.fail_send = False
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PlatformError) as download_error:
            await platform.download(
                Attachment("file-id", "document"), chat=ChatRef("tg", "-100"), max_bytes=100
            )
    assert token not in caplog.text
    assert token not in str(download_error.value)
    assert token not in repr(download_error.value)
    assert token not in repr(download_error.value.__cause__)
    assert token not in repr(download_error.value.__context__)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("creator", True),
        ("administrator", True),
        ("member", False),
        ("left", False),
        ("restricted", False),
    ],
)
async def test_live_group_admin_roles(status: str, expected: bool) -> None:
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(status=status)
    platform = make_platform(bot)
    assert await platform.is_group_admin(ChatRef("tg", "-100"), UserRef("tg", "7")) is expected
    bot.get_chat_member.assert_awaited_once_with("-100", 7)


async def test_live_group_admin_rechecks_role_after_revocation() -> None:
    bot = AsyncMock()
    bot.get_chat_member.side_effect = [
        SimpleNamespace(status="administrator"),
        SimpleNamespace(status="member"),
    ]
    platform = make_platform(bot)
    chat, actor = ChatRef("tg", "-100"), UserRef("tg", "7")
    assert await platform.is_group_admin(chat, actor)
    assert not await platform.is_group_admin(chat, actor)
    assert bot.get_chat_member.await_count == 2


async def test_live_group_admin_fails_closed_on_namespace_and_lookup_error() -> None:
    bot = AsyncMock()
    bot.get_chat_member.side_effect = RuntimeError("private diagnostic")
    platform = make_platform(bot)
    assert not await platform.is_group_admin(ChatRef("console", "-100"), UserRef("tg", "7"))
    assert not await platform.is_group_admin(ChatRef("tg", "-100"), UserRef("other", "7"))
    bot.get_chat_member.assert_not_awaited()
    from chatcore.platform import PlatformError

    with pytest.raises(PlatformError, match="RuntimeError"):
        await platform.is_group_admin(ChatRef("tg", "-100"), UserRef("tg", "7"))


@pytest.mark.parametrize("anonymous", [False, True])
async def test_native_poll_send_and_stop_keep_topic_and_explicit_anonymity(anonymous):
    from chatcore.polls import PollPlatform, PollResult, SentPoll
    from telegram.constants import PollLimit

    bot = AsyncMock()
    bot.send_poll.return_value = SimpleNamespace(message_id=42, poll=SimpleNamespace(id="poll-id"))
    bot.stop_poll.return_value = SimpleNamespace(
        options=[SimpleNamespace(voter_count=3), SimpleNamespace(voter_count=2)],
        total_voter_count=4,
    )
    platform = make_platform(bot)
    assert isinstance(platform, PollPlatform)
    assert platform.poll_limits.max_options == PollLimit.MAX_OPTION_NUMBER
    chat = ChatRef("tg", "-100")
    sent = await platform.send_poll(
        chat, "Dinner?", ["Ramen", "Tacos"], multiple=True, anonymous=anonymous, thread_id="7"
    )
    assert sent == SentPoll(MessageRef(chat, "42", "7"), "poll-id")
    bot.send_poll.assert_awaited_once_with(
        chat_id=chat.id,
        question="Dinner?",
        options=["Ramen", "Tacos"],
        is_anonymous=anonymous,
        allows_multiple_answers=True,
        type="regular",
        message_thread_id=7,
    )
    assert await platform.stop_poll(sent.message) == PollResult((3, 2), 4)
    bot.stop_poll.assert_awaited_once_with(chat_id=chat.id, message_id=42)


@pytest.mark.parametrize(("thread_id", "expected_thread"), [("7", 7), (None, None)])
async def test_send_file_uses_document_and_preserves_topic(thread_id, expected_thread):
    from chatcore.files import FilePlatform

    bot = AsyncMock()
    bot.send_document.return_value = SimpleNamespace(message_id=42)
    platform = make_platform(bot)
    chat = ChatRef("tg", "-100")

    assert isinstance(platform, FilePlatform)
    sent = await platform.send_file(
        chat, "expenses.csv", b"csv data", caption="Japan ledger", thread_id=thread_id
    )

    assert sent == MessageRef(chat, "42", thread_id)
    bot.send_document.assert_awaited_once_with(
        chat_id=chat.id,
        document=b"csv data",
        filename="expenses.csv",
        caption="Japan ledger",
        message_thread_id=expected_thread,
    )


async def test_send_file_rejects_payload_over_telegram_limit():
    from chatcore_telegram import MAX_FILE_BYTES

    bot = AsyncMock()
    platform = make_platform(bot)

    with pytest.raises(PlatformError, match="FileTooLarge"):
        await platform.send_file(ChatRef("tg", "-100"), "too-big.csv", b"x" * (MAX_FILE_BYTES + 1))

    bot.send_document.assert_not_awaited()


@pytest.mark.parametrize("indices", [[], [0], [0, 1]])
async def test_poll_answer_maps_native_updates_including_retractions(indices):
    from chatcore.events import PollAnswer
    from telegram import PollAnswer as TelegramPollAnswer

    platform = make_platform()
    platform._handler = AsyncMock()
    await platform._handle_update(
        Update(
            1,
            poll_answer=TelegramPollAnswer(
                "poll-id", indices, user=user(), option_persistent_ids=[]
            ),
        )
    )
    platform._handler.assert_awaited_once_with(
        PollAnswer("poll-id", platform._user(user()), tuple(indices))
    )


async def test_poll_answer_from_chat_is_ignored():
    from telegram import PollAnswer as TelegramPollAnswer

    platform = make_platform()
    platform._handler = AsyncMock()
    await platform._handle_update(
        Update(
            1,
            poll_answer=TelegramPollAnswer(
                "poll-id", [0], voter_chat=Chat(-100, "supergroup"), option_persistent_ids=[]
            ),
        )
    )
    platform._handler.assert_not_awaited()


async def test_native_poll_retry_and_safe_stop_failure(monkeypatch):
    bot = AsyncMock()
    bot.send_poll.side_effect = [
        RetryAfter(1),
        SimpleNamespace(message_id=42, poll=SimpleNamespace(id="poll-id")),
    ]
    bot.stop_poll.side_effect = BadRequest("private content")
    sleep = AsyncMock()
    monkeypatch.setattr("chatcore_telegram.asyncio.sleep", sleep)
    platform = make_platform(bot)
    sent = await platform.send_poll(
        ChatRef("tg", "-100"), "Dinner?", ["A", "B"], multiple=False, anonymous=False
    )
    assert bot.send_poll.await_count == 2
    sleep.assert_awaited_once_with(1)
    with pytest.raises(PlatformError, match="^BadRequest$"):
        await platform.stop_poll(sent.message)


@pytest.mark.parametrize("retries", [0, 3])
async def test_download_gives_up_after_configured_retries(monkeypatch, retries) -> None:
    bot = AsyncMock()
    bot.get_file.side_effect = TimedOut("down")
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("chatcore_telegram.asyncio.sleep", sleep)
    monkeypatch.setattr("chatcore_telegram.random.uniform", lambda *_: 0)
    platform = TelegramPlatform(
        TelegramSettings(bot_token=SecretStr("fake-token"), download_retries=retries),
        request=FakeRequest(),
    )
    platform._application.bot = bot
    with pytest.raises(PlatformError, match="TimedOut"):
        await platform.download(
            Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10
        )
    assert bot.get_file.await_count == 1 + retries
    assert sleeps == [0.5, 1.0, 2.0][:retries]


async def test_download_total_backoff_is_capped(monkeypatch) -> None:
    bot = AsyncMock()
    file = SimpleNamespace(file_size=2, download_as_bytearray=AsyncMock(return_value=b"ok"))
    bot.get_file.side_effect = [TimedOut("a"), TimedOut("b"), TimedOut("c"), file]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("chatcore_telegram.asyncio.sleep", sleep)
    monkeypatch.setattr("chatcore_telegram.random.uniform", lambda *_: 0)
    platform = TelegramPlatform(
        TelegramSettings(
            bot_token=SecretStr("fake-token"), download_retries=3, download_backoff_s=5
        ),
        request=FakeRequest(),
    )
    platform._application.bot = bot
    assert (
        await platform.download(
            Attachment("f", "document"), chat=ChatRef("tg", "-100"), max_bytes=10
        )
        == b"ok"
    )
    assert sleeps == [5, 3, 0] and sum(sleeps) <= 8


@pytest.mark.parametrize(
    "field", [{"download_retries": 6}, {"download_retries": -1}, {"download_backoff_s": 6}]
)
def test_download_retry_settings_are_bounded(field) -> None:
    with pytest.raises(ValueError):
        TelegramSettings(bot_token=SecretStr("fake-token"), **field)


async def test_send_reply_prompt_adds_selective_force_reply_to_the_last_part() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = [SimpleNamespace(message_id=i) for i in (1, 2)]
    platform = make_platform(bot)
    chat = ChatRef("tg", "-100")
    ref = await platform.send_reply_prompt(
        chat,
        "a" * 4095 + "\nb",
        reply_to=MessageRef(chat, "5"),
        thread_id="42",
        placeholder="Model name or default",
    )
    first, last = bot.send_message.await_args_list
    assert "reply_markup" not in first.kwargs
    markup = last.kwargs["reply_markup"]
    assert markup.selective is True
    assert markup.input_field_placeholder == "Model name or default"
    assert first.kwargs["reply_parameters"].message_id == 5
    assert last.kwargs["message_thread_id"] == 42
    assert ref.message_id == "2"
    assert platform.sent_message_ids(ref) == ("1", "2")


async def test_plain_send_has_no_reply_markup() -> None:
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=1)
    platform = make_platform(bot)
    await platform.send(ChatRef("tg", "-100"), "hi")
    assert "reply_markup" not in bot.send_message.await_args.kwargs
