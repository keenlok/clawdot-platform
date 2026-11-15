import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from chatcore.events import ChatRef
from chatcore_telegram import TelegramPlatform, TelegramSettings
from telegram.error import BadRequest


def platform():
    return TelegramPlatform(TelegramSettings(bot_token="fake-token"))


async def test_chat_title_returns_group_title_without_logging_it(caplog, monkeypatch):
    adapter = platform()
    monkeypatch.setattr(
        type(adapter._application.bot),
        "get_chat",
        AsyncMock(return_value=SimpleNamespace(type="supergroup", title="Secret Crew Name")),
    )
    with caplog.at_level(logging.DEBUG):
        assert await adapter.chat_title(ChatRef("tg", "-100")) == "Secret Crew Name"
    assert "Secret Crew Name" not in caplog.text


async def test_chat_title_returns_none_on_telegram_error(monkeypatch):
    adapter = platform()
    monkeypatch.setattr(
        type(adapter._application.bot), "get_chat", AsyncMock(side_effect=BadRequest("failure"))
    )
    assert await adapter.chat_title(ChatRef("tg", "-100")) is None
