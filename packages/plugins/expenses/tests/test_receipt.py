import base64
import json
import logging
from datetime import date, datetime

import pytest
from chatcore.events import Attachment, AttachmentKind
from chatcore.image_reader import ImageReader
from chatcore.llm.base import ProviderError
from chatcore.llm.types import Completion, ImagePart, Message, Usage
from chatcore.testing import GROUP, FakePlatform, memory_stores, message, user
from chatcore.tools.registry import ToolContext
from chatcore_expenses.plugin import ExpensesPlugin
from chatcore_expenses.receipt import (
    _INSTRUCTION,
    _SYSTEM,
    ReceiptError,
    ReceiptReader,
    parse_receipt,
)

TODAY = date(2026, 10, 31)


def valid(**overrides):
    data = {
        "merchant": "Cafe",
        "date": "2026-10-20",
        "currency": "SGD",
        "items": [{"description": "Coffee", "quantity": "1", "amount": "4.00"}],
        "subtotal": "4.00",
        "tax": "0.00",
        "service_charge": None,
        "discount": None,
        "tip": None,
        "total": "4.00",
    }
    data.update(overrides)
    return json.dumps(data)


def test_parse_receipt_clean_and_fenced_json():
    assert parse_receipt(valid(), today=TODAY).total == "4.00"
    assert parse_receipt("  ```json\n" + valid() + "\n```  ", today=TODAY).merchant == "Cafe"
    assert parse_receipt("  ```\n" + valid() + "\n```  ", today=TODAY).merchant == "Cafe"


def test_leap_day_cutoff_keeps_feb_28_and_warns_on_feb_27():
    kept = parse_receipt(valid(date="2028-02-28"), today=date(2028, 2, 29))
    warned = parse_receipt(valid(date="2026-02-27"), today=date(2028, 2, 29))
    assert kept.date == "2028-02-28"
    assert warned.date is None and warned.warnings


def test_amount_parser_distinguishes_decimal_and_thousands_commas():
    decimal_comma = parse_receipt(valid(total="12,50", items=[]), today=TODAY)
    thousands_integer = parse_receipt(valid(total="1,234", items=[]), today=TODAY)
    thousands_decimal = parse_receipt(valid(total="1,234.50", items=[]), today=TODAY)
    grouped_decimal_comma = parse_receipt(valid(total="1 234,50", items=[]), today=TODAY)
    nbsp_decimal_comma = parse_receipt(valid(total="1\u00a0234,50", items=[]), today=TODAY)
    assert decimal_comma.total == "12.50"
    assert thousands_integer.total == "1234.00"
    assert thousands_decimal.total == "1234.50"
    assert grouped_decimal_comma.total == "1234.50"
    assert nbsp_decimal_comma.total == "1234.50"
    assert parse_receipt(valid(total="1\u202f234,50", items=[]), today=TODAY).total == "1234.50"
    assert parse_receipt(valid(total="1\u2009234,50", items=[]), today=TODAY).total == "1234.50"
    assert parse_receipt(valid(total="1'234.50", items=[]), today=TODAY).total == "1234.50"


@pytest.mark.parametrize("raw", ["words " + valid(), "[]", '{"error":"not_a_receipt"}', "not json"])
def test_parse_receipt_rejects_non_receipt_responses(raw):
    with pytest.raises(ReceiptError):
        parse_receipt(raw, today=TODAY)


@pytest.mark.parametrize("total", [None, "0", "-1", "1000001", "NaN"])
def test_parse_receipt_requires_bounded_positive_total(total):
    data = json.loads(valid())
    data["total"] = total
    with pytest.raises(ReceiptError):
        parse_receipt(json.dumps(data), today=TODAY)


def test_amounts_accept_numbers_currency_symbols_and_unknown_currency_warns():
    receipt = parse_receipt(
        valid(
            currency="sgd",
            items=[{"description": "Coffee", "amount": 1234.5}],
            subtotal="S$1,234.50",
            total="S$1,234.50",
        ),
        today=TODAY,
    )
    assert receipt.total == "1234.50"
    unknown = parse_receipt(valid(currency="ZZZ"), today=TODAY)
    assert unknown.currency is None and unknown.warnings


def test_jpy_uses_zero_decimal_formatting():
    receipt = parse_receipt(
        valid(
            currency="JPY",
            items=[{"description": "Tea", "amount": "100"}],
            subtotal="100",
            total="100",
        ),
        today=TODAY,
    )
    assert receipt.total == "100"


def test_bad_dates_are_dropped_with_warning_and_sums_are_checked():
    future = parse_receipt(valid(date="2027-01-01"), today=TODAY)
    ancient = parse_receipt(valid(date="2020-01-01"), today=TODAY)
    mismatch = parse_receipt(valid(total="8.00"), today=TODAY)
    assert future.date is None and ancient.date is None
    assert future.warnings and ancient.warnings
    assert any("add up" in warning for warning in mismatch.warnings)


def test_tax_exclusive_and_inclusive_receipts_do_not_warn():
    exclusive = parse_receipt(
        valid(
            items=[{"description": "Meal", "amount": "10"}],
            subtotal="10",
            tax="0.80",
            total="10.80",
        ),
        today=TODAY,
    )
    inclusive = parse_receipt(
        valid(
            items=[{"description": "Meal", "amount": "10"}], subtotal="10", tax="0.80", total="10"
        ),
        today=TODAY,
    )
    assert not any("add up" in warning for warning in exclusive.warnings + inclusive.warnings)


def test_items_and_strings_are_capped_and_sanitized():
    data = json.loads(
        valid(
            merchant="A\x00   " + "m" * 90,
            items=[{"description": "D\x01   " + "x" * 90, "amount": "1"}] * 60,
            total="60",
        )
    )
    receipt = parse_receipt(json.dumps(data), today=TODAY)
    assert len(receipt.merchant) <= 80 and "\x00" not in receipt.merchant
    assert len(receipt.items) == 50 and len(receipt.items[0].description) <= 60
    assert receipt.warnings


class VisionFake:
    name = "vision-fake"

    def __init__(self, output=None, error=False):
        self.output = output or valid()
        self.error = error
        self.calls = []

    async def complete(
        self, messages, *, system, tools=(), tool_choice="auto", max_tokens=2048, session_key=None
    ):
        self.calls.append((list(messages), system, list(tools), max_tokens, session_key))
        if self.error:
            raise ProviderError("private response")
        return Completion(Message.assistant(self.output), "end_turn", Usage(12, 5))


def reader(provider):
    return ReceiptReader(ImageReader(provider, model="vision", label="vision on test"))


async def test_receipt_reader_accepts_injected_singapore_today():
    vision = VisionFake(output=valid(date="2026-11-01"))
    image = ImagePart(b"\xff\xd8\xffimage", "image/jpeg")
    receipt, _usage = await reader(vision).read(
        image,
        session_key="fake:receipt",
        today=date(2026, 11, 1),
    )
    assert receipt.date == "2026-11-01"
    messages, system, tools, _max_tokens, _session_key = vision.calls[0]
    assert messages == [Message.user(_INSTRUCTION, image)]
    assert system == _SYSTEM and tools == []


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("max_tokens", "I couldn't read that receipt right now."),
        ("end_turn", "I couldn't read the receipt details."),
    ],
)
async def test_receipt_reader_maps_incomplete_and_empty_with_usage(stop_reason, expected):
    class ResultProvider(VisionFake):
        async def complete(self, messages, **kwargs):
            self.calls.append(
                (
                    list(messages),
                    kwargs["system"],
                    list(kwargs.get("tools", ())),
                    kwargs["max_tokens"],
                    kwargs.get("session_key"),
                )
            )
            return Completion(Message.assistant(""), stop_reason, Usage(4, 2))

    with pytest.raises(ReceiptError, match=expected) as error:
        await reader(ResultProvider()).read(
            ImagePart(b"\xff\xd8\xffimage", "image/jpeg"),
            session_key="fake:receipt",
            today=TODAY,
        )
    assert error.value.usage == Usage(4, 2)


class LimitFake:
    class Settings:
        DAILY_TOKEN_BUDGET = 100

    settings = Settings()

    def __init__(self, used=0):
        self.used = used
        self.recorded = []

    async def budget_used(self):
        return self.used

    async def budget_exhausted(self):
        return self.used >= self.settings.DAILY_TOKEN_BUDGET

    async def record(self, count):
        self.recorded.append(count)


class CountingPlatform(FakePlatform):
    downloads: int = 0

    async def download(self, attachment, *, chat, max_bytes):
        self.downloads += 1
        return await super().download(attachment, chat=chat, max_bytes=max_bytes)


def receipt_ctx(plugin, *, platform=None, stores=None, sender=None, attachments=()):
    from chatcore.tools.registry import TurnState

    platform = platform or FakePlatform()
    stores = stores or memory_stores()
    ctx = ToolContext(GROUP, sender or user(), False, platform, stores)
    ctx.turn = TurnState(attachments=tuple(attachments))
    return ctx


async def test_read_receipt_registers_only_with_reader_and_is_read_only():
    assert "read_receipt" not in {tool.name for tool in ExpensesPlugin().tools()}
    plugin = ExpensesPlugin(receipt_reader=reader(VisionFake()))
    tool = next(tool for tool in plugin.tools() if tool.name == "read_receipt")
    assert tool.writes is False and tool.admin_only is False


async def test_read_receipt_returns_untrusted_fields_and_sends_one_sniffed_image():
    vision = VisionFake()
    limits = LimitFake()
    plugin = ExpensesPlugin(receipt_reader=reader(vision), limits=limits)
    attachment = Attachment("receipt", AttachmentKind.PHOTO)
    platform = FakePlatform(files={"receipt": b"\xff\xd8\xffphoto"})
    ctx = receipt_ctx(plugin, platform=platform, attachments=[attachment])
    result = await plugin.tools()[0].handler(ctx)
    assert result.untrusted and result.pending_action is None
    assert '"total":"4.00"' in result.content
    assert vision.output not in result.content
    request, _system, tools, _max_tokens, session = vision.calls[0]
    assert tools == [] and session == f"{GROUP.key}:receipt"
    assert request[0].content[1] == ImagePart(b"\xff\xd8\xffphoto", "image/jpeg")
    assert limits.recorded == [17]


async def test_read_receipt_reuses_turn_download_and_failed_download_does_not_spend_quota():
    plugin = ExpensesPlugin(receipt_reader=reader(VisionFake()))
    attachment = Attachment("receipt", AttachmentKind.PHOTO)
    platform = CountingPlatform(files={"receipt": b"\xff\xd8\xffphoto"})
    ctx = receipt_ctx(plugin, platform=platform, attachments=[attachment])
    ctx.turn.downloads[attachment.id] = b"\xff\xd8\xffphoto"
    result = await next(t for t in plugin.tools() if t.name == "read_receipt").handler(ctx)
    assert result.untrusted and platform.downloads == 0

    class BrokenPlatform(CountingPlatform):
        async def download(self, attachment, *, chat, max_bytes):
            self.downloads += 1
            raise RuntimeError("private transport detail")

    broken = BrokenPlatform()
    failed_ctx = receipt_ctx(plugin, platform=broken, attachments=[attachment])
    failed = await next(t for t in plugin.tools() if t.name == "read_receipt").handler(failed_ctx)
    assert failed.content == "I couldn't download that photo from Telegram. Please send it again."
    day = datetime.now(plugin.timezone).date().isoformat()
    key = f"receipt:user:{failed_ctx.user.ref.key}:{day}"
    assert await failed_ctx.stores.cache.get(key) is None


async def test_visible_receipt_reader_failure_is_only_an_instruction_to_the_model():
    vision = VisionFake(error=True)
    plugin = ExpensesPlugin(receipt_reader=reader(vision))
    attachment = Attachment("receipt", AttachmentKind.PHOTO)
    platform = CountingPlatform()
    ctx = receipt_ctx(plugin, platform=platform, attachments=[attachment])
    ctx.turn.vision = True
    ctx.turn.downloads[attachment.id] = b"\xff\xd8\xffphoto"
    result = await next(t for t in plugin.tools() if t.name == "read_receipt").handler(ctx)
    assert result.is_error
    assert result.content == (
        "The receipt reader couldn't process this image, but you can see it. Read the total, "
        "currency and date from the image yourself and continue (call add_expense). "
        "Don't mention this to the user."
    )
    assert platform.downloads == 0


async def test_read_receipt_uses_runtime_budget_override():
    from chatcore.limits import BudgetOverride, Limits, LimitsSettings
    from chatcore.store.memory import MemoryCache

    limits = Limits(
        MemoryCache(),
        LimitsSettings(DAILY_TOKEN_BUDGET=100_000, BUDGET_TZ="UTC"),
        budget_override=lambda: BudgetOverride(200_000, None, None, "fake:owner", None),
    )
    await limits.record(150_000)
    vision = VisionFake()
    plugin = ExpensesPlugin(receipt_reader=reader(vision), limits=limits)
    attachment = Attachment("receipt", AttachmentKind.PHOTO)
    platform = FakePlatform(files={"receipt": b"\xff\xd8\xffphoto"})
    result = await next(t for t in plugin.tools() if t.name == "read_receipt").handler(
        receipt_ctx(plugin, platform=platform, attachments=[attachment])
    )
    assert result.untrusted
    assert len(vision.calls) == 1


async def test_read_receipt_requires_files_images_and_valid_index():
    plugin = ExpensesPlugin(receipt_reader=reader(VisionFake()))
    tool = next(tool for tool in plugin.tools() if tool.name == "read_receipt")
    no_files = FakePlatform()
    no_files.capabilities = type(no_files.capabilities)(False, True, True, False, True, 4096)
    result = await tool.handler(receipt_ctx(plugin, platform=no_files))
    assert result.content == "This chat platform can't share images."
    result = await tool.handler(receipt_ctx(plugin))
    assert result.is_error and "send the receipt" in result.content
    ctx = receipt_ctx(plugin, attachments=[Attachment("x", AttachmentKind.PHOTO)])
    result = await tool.handler(ctx, image=2)
    assert result.is_error and "1 image" in result.content


async def test_receipt_limit_is_per_user_and_budget_blocks_before_download():
    plugin = ExpensesPlugin(receipt_reader=reader(VisionFake()))
    attachment = Attachment("receipt", AttachmentKind.PHOTO)
    platform = CountingPlatform(files={"receipt": b"\xff\xd8\xffphoto"})
    ctx = receipt_ctx(plugin, platform=platform, attachments=[attachment])
    tool = next(t for t in plugin.tools() if t.name == "read_receipt")
    for _ in range(10):
        assert (await tool.handler(ctx)).untrusted
    vision = plugin.receipt_reader.reader.provider
    assert platform.downloads == 1 and len(vision.calls) == 10
    assert (await tool.handler(ctx)).content == "You've reached today's receipt-reading limit."
    assert platform.downloads == 1 and len(vision.calls) == 10
    other = receipt_ctx(plugin, platform=platform, attachments=[attachment], sender=user("bob"))
    assert (await tool.handler(other)).untrusted

    blocked_vision = VisionFake()
    blocked_limits = LimitFake(used=100)
    blocked_plugin = ExpensesPlugin(receipt_reader=reader(blocked_vision), limits=blocked_limits)
    blocked_platform = CountingPlatform(files={"receipt": b"\xff\xd8\xffphoto"})
    blocked_ctx = receipt_ctx(blocked_plugin, platform=blocked_platform, attachments=[attachment])
    result = await next(t for t in blocked_plugin.tools() if t.name == "read_receipt").handler(
        blocked_ctx
    )
    assert "token budget" in result.content
    assert blocked_platform.downloads == 0 and blocked_vision.calls == []


async def test_receipt_provider_error_is_generic_and_does_not_leak_image_or_output(caplog):
    vision = VisionFake(error=True)
    plugin = ExpensesPlugin(receipt_reader=reader(vision))
    raw_image = b"\xff\xd8\xffprivate-image"
    platform = FakePlatform(files={"receipt": raw_image})
    ctx = receipt_ctx(
        plugin, platform=platform, attachments=[Attachment("receipt", AttachmentKind.PHOTO)]
    )
    result = await next(t for t in plugin.tools() if t.name == "read_receipt").handler(ctx)
    assert (
        result.is_error
        and result.content == "I couldn't read that receipt right now. Please try again later."
    )
    assert "private-image" not in result.content and "private response" not in caplog.text


async def test_agent_receipt_reader_failure_recovers_from_visible_image_and_commits_directly():
    from chatcore.access import AccessPolicy
    from chatcore.agent import AgentResponder, AgentSettings
    from chatcore.app import BotApp
    from chatcore.llm.types import ToolCall
    from chatcore.testing import ScriptedProvider
    from chatcore_expenses.repository import MemoryLedgerRepo

    class Limits:
        settings = type("Settings", (), {"DAILY_TOKEN_BUDGET": 1000})()

        async def check(self, *_args):
            return None

        async def budget_used(self):
            return 0

        async def budget_exhausted(self):
            return False

        async def record(self, _tokens):
            return None

    stores = memory_stores()
    alice = user("alice")
    await stores.repo.upsert_member(GROUP, alice)
    ledger = MemoryLedgerRepo()
    vision = VisionFake(error=True)
    expenses = ExpensesPlugin(ledger=ledger, receipt_reader=reader(vision))
    main = ScriptedProvider(
        [
            Completion(
                Message.assistant("", (ToolCall("read", "read_receipt", {"image": 1}),)), "tool_use"
            ),
            Completion(
                Message.assistant(
                    "",
                    (
                        ToolCall(
                            "add",
                            "add_expense",
                            {
                                "description": "Cafe",
                                "amount": "4.00",
                                "currency": "SGD",
                                "paid_by": "me",
                                "split_with": ["me"],
                                "spent_on": "2026-10-20",
                            },
                        ),
                    ),
                ),
                "tool_use",
            ),
            Completion(Message.assistant(""), "end_turn"),
        ]
    )
    platform = CountingPlatform(files={"receipt": b"\xff\xd8\xffphoto"})
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[expenses],
        responder=AgentResponder(
            main, system_prompt="rules", settings=AgentSettings(vision=True), limits=Limits()
        ),
        bot_name="bot",
    )
    incoming = message(
        "@bot read this receipt and add it",
        mention=True,
        attachments=[Attachment("receipt", AttachmentKind.PHOTO)],
    )
    await app.handle(incoming)
    assert main.calls[0]["tools"]
    assert any(
        isinstance(part, ImagePart)
        for request in main.calls[0]["messages"]
        for part in request.content
    )
    assert len(vision.calls) == 1 and vision.calls[0][2] == []
    assert len(await ledger.expenses(GROUP)) == 1
    assert platform.downloads == 1
    assert not stores.repo._pending
    assert any(sent.text.startswith("Added: Cafe") for sent in platform.sent)
    assert not any("couldn't process this image" in sent.text for sent in platform.sent)


@pytest.mark.parametrize("image", ["1", 1.5, True])
async def test_read_receipt_rejects_non_integer_image_argument(image):
    vision = VisionFake()
    platform = CountingPlatform(files={"receipt": b"\xff\xd8\xffphoto"})
    plugin = ExpensesPlugin(receipt_reader=reader(vision))
    ctx = receipt_ctx(
        plugin, platform=platform, attachments=[Attachment("receipt", AttachmentKind.PHOTO)]
    )
    tool = next(t for t in plugin.tools() if t.name == "read_receipt")
    result = await tool.handler(ctx, image=image)
    assert result.is_error
    assert platform.downloads == 0 and vision.calls == []


async def test_successful_receipt_read_does_not_log_image_or_extracted_data(caplog):
    merchant = "MERCHANT_SENTINEL"
    raw = (
        '{"merchant":"MERCHANT_SENTINEL","date":"2026-10-20","currency":"SGD",'
        '"items":[{"description":"ITEM_SENTINEL","amount":"123.45"}],"total":"123.45"}'
    )
    vision = VisionFake(output=raw)
    image_sentinel = b"IMAGE_SENTINEL"
    image_bytes = b"\xff\xd8\xff" + image_sentinel
    platform = CountingPlatform(files={"receipt": image_bytes})
    plugin = ExpensesPlugin(receipt_reader=reader(vision))
    ctx = receipt_ctx(
        plugin, platform=platform, attachments=[Attachment("receipt", AttachmentKind.PHOTO)]
    )
    caplog.set_level(logging.INFO)
    result = await next(t for t in plugin.tools() if t.name == "read_receipt").handler(ctx)
    logged = caplog.text
    image_b64 = base64.b64encode(image_bytes).decode()
    image_forms = (
        repr(image_bytes),
        image_bytes.decode("latin1"),
        image_bytes.hex(),
        image_b64,
        image_sentinel.decode(),
    )
    assert all(form not in logged and form not in result.content for form in image_forms)
    assert merchant not in logged
    assert "123.45" not in logged and "ITEM_SENTINEL" not in logged
    assert raw not in logged and image_bytes.decode("latin1") not in logged
    assert image_b64 not in logged
    assert merchant in result.content and "123.45" in result.content
    assert image_bytes.decode("latin1") not in result.content and image_b64 not in result.content
