"""Cards use the same Telegram photo and album contract as generated images."""

import json
import struct
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from chatcore.access import AccessPolicy
from chatcore.cards import validate_card
from chatcore.events import ChatRef, User, UserRef
from chatcore.testing import memory_stores
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_cards import CardsPlugin, CardsSettings
from chatcore_cards.service import CardService
from chatcore_telegram import TelegramPlatform, TelegramSettings
from pydantic import SecretStr


def png(width, height):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    scanline = b"\0" + b"\xff\xff\xff\xff" * width
    raw = zlib.compress(scanline * height, 9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", raw)
        + chunk(b"IEND", b"")
    )


class Renderer:
    def __init__(self, pages):
        self.pages = pages

    async def render(self, card, *, theme="light", footer=None):
        return self.pages

    async def aclose(self):
        pass


async def test_send_card_routes_single_png_and_album_through_telegram():
    bot = AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(message_id=1)
    bot.send_media_group.return_value = [
        SimpleNamespace(message_id=2),
        SimpleNamespace(message_id=3),
        SimpleNamespace(message_id=4),
    ]
    platform = TelegramPlatform(TelegramSettings(bot_token=SecretStr("fake-token")))
    platform._application = SimpleNamespace(bot=bot)
    access = AccessPolicy(["tg:group"], [])
    user = User(UserRef("tg", "user"), display_name="Alex")
    ctx = ToolContext(
        ChatRef("tg", "group"), user, False, platform, memory_stores(), turn=TurnState()
    )
    settings = CardsSettings(CARDS_ENABLED=True, CARDS_RENDER_URL="http://renderer")
    for pages in ([png(1000, 9000)], [png(1000, 1000)] * 3):
        plugin = CardsPlugin(
            access,
            platform,
            settings=settings,
            service=CardService(settings, client=Renderer(pages)),
        )
        tool = plugin.tools()[0]
        result = await ToolRegistry([tool]).call(
            tool.name, {"title": "Summary", "blocks": [], "caption": "Summary"}, ctx
        )
        assert not result.is_error
        assert ctx.turn.tool_attempts == {"send_card": 1}
        ctx.turn = TurnState()
    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["caption"] == "📋 Summary"
    bot.send_media_group.assert_awaited_once()
    media = bot.send_media_group.await_args.kwargs["media"]
    assert len(media) == 3
    assert media[0].caption == "📋 Summary"
    assert media[1].caption is None and media[2].caption is None


def test_true_maximum_english_and_chinese_payloads_fit_skill_output_budget():
    def arguments(fill):
        base = "https://source.example/"
        url = base + "x" * (300 - len(base))
        table = {
            "type": "table",
            "columns": [{"header": fill * 24, "align": "start"} for _ in range(5)],
            "rows": [[fill] * 5 for _ in range(40)],
            "total": [fill * 60 for _ in range(5)],
        }
        blocks = [
            {
                "type": "stats",
                "tiles": [
                    {"label": fill * 24, "value": fill * 18, "note": fill * 48} for _ in range(4)
                ],
            },
            {"type": "heading", "text": fill * 80},
            {"type": "paragraph", "text": fill * 1000},
            {"type": "bullets", "items": [fill * 200 for _ in range(12)]},
            table,
            {"type": "callout", "tone": "info", "text": fill * 300},
            {
                "type": "sources",
                "items": [{"title": fill * 100, "url": url} for _ in range(8)],
            },
            *[{"type": "heading", "text": fill * 80} for _ in range(9)],
        ]
        card = {"title": fill * 80, "subtitle": fill * 120, "blocks": blocks}

        def count(value, key=None):
            if isinstance(value, str):
                return 0 if key == "url" else len(value)
            if isinstance(value, dict):
                return sum(count(child, name) for name, child in value.items())
            if isinstance(value, list):
                return sum(count(child) for child in value)
            return 0

        remaining = CardsSettings().CARDS_MAX_TEXT_CHARS - count(card)
        cells = range(len(table["rows"]) * len(table["columns"]))
        for index in cells:
            extra = min(59, remaining)
            row_index, cell_index = divmod(index, 5)
            table["rows"][row_index][cell_index] += fill * extra
            remaining -= extra
        assert remaining == 0
        checked = validate_card(card, max_text_chars=CardsSettings().CARDS_MAX_TEXT_CHARS)
        assert len(checked.blocks) == 16
        assert len(checked.blocks[4].rows) == 40
        assert len(checked.blocks[4].columns) == 5
        assert len(checked.blocks[6].items) == 8
        assert all(len(item.url) == 300 for item in checked.blocks[6].items)
        assert count(card) == CardsSettings().CARDS_MAX_TEXT_CHARS
        return {**card, "caption": fill * 200}

    english_json = json.dumps(arguments("x"), ensure_ascii=False)
    chinese_json = json.dumps(arguments("中"), ensure_ascii=False)
    assert len(english_json) / 2.5 <= 12288
    assert len(chinese_json) <= 12288 * 1.3


def _flatten_strings(value, field=""):
    if isinstance(value, str):
        return [(field, value)]
    if isinstance(value, dict):
        return [item for key, child in value.items() for item in _flatten_strings(child, key)]
    if isinstance(value, list):
        return [item for child in value for item in _flatten_strings(child, field)]
    return []
