from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from chatcore.access import AccessPolicy
from chatcore.agent import MODEL_SWITCH_PROMPT, AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.embed import FakeEmbedder
from chatcore.events import Attachment, AttachmentKind
from chatcore.image_reader import ImageReader, ImageReading
from chatcore.llm.base import ProviderError
from chatcore.llm.types import Completion, ImagePart, Message, ToolCall, Usage
from chatcore.model_choice import build_model_switch
from chatcore.testing import GROUP, FakePlatform, ScriptedProvider, memory_stores, message
from chatcore.tools.registry import Tool, ToolResult

JPEG = b"\xff\xd8\xffjpeg"


@dataclass
class ModelProvider(ScriptedProvider):
    """A scripted provider with a ``model`` and ``with_model`` views sharing its queues."""

    model: str = "default-model"

    def with_model(self, model: str) -> ModelProvider:
        view = copy.copy(self)
        view.model = model
        return view

    async def complete(self, messages, **kwargs):
        result = await super().complete(messages, **kwargs)
        self.calls[-1]["model"] = self.model
        return result


class Limits:
    def __init__(self) -> None:
        self.recorded: list[int] = []
        self.admissions = 0
        self.exhausted = False

    async def check(self, *_args):
        return None

    async def record(self, tokens):
        self.recorded.append(tokens)

    async def budget_exhausted(self):
        self.admissions += 1
        return self.exhausted


def _app(
    provider,
    *,
    vision=True,
    choices="glm-5,kimi:text",
    configured=True,
    image_reader=None,
    describe_per_user_per_day=30,
    extra_plugins=(),
):
    platform = FakePlatform(files={"photo": JPEG})
    stores = memory_stores()
    switch = (
        build_model_switch(
            provider_kind="openai_compat",
            default_model="default-model",
            choices_raw=choices,
            cache=stores.cache,
            duration_minutes=30,
            max_minutes=60,
            cooldown_s=0,
            who="anyone",
            clock=lambda: datetime.now(UTC),
        )
        if configured
        else None
    )
    conversation = ConversationPlugin()
    limits = Limits()
    responder = AgentResponder(
        provider,
        system_prompt="rules",
        settings=AgentSettings(vision=vision, recent_context_messages=0),
        limits=limits,
        model_switch=switch,
        image_reader=image_reader,
        describe_per_user_per_day=describe_per_user_per_day,
    )
    app = BotApp(
        platform,
        stores=stores,
        access=AccessPolicy([GROUP.key], []),
        plugins=[conversation, *extra_plugins],
        bot_name="bot",
        responder=responder,
    )
    usage_rows = []

    async def record_usage(chat, user_ref, provider_name, model, input_tokens, output_tokens):
        usage_rows.append((provider_name, model, input_tokens, output_tokens))

    conversation.store.record_usage = record_usage
    return app, switch, limits, usage_rows


async def test_turn_uses_the_override_model_and_records_it(caplog):
    provider = ModelProvider([Completion(Message.assistant("hi"), "end_turn", Usage(3, 2))])
    app, switch, _limits, usage_rows = _app(provider)
    await switch.store.switch(GROUP, "glm-5", set_by="Alice")

    with caplog.at_level(logging.INFO, logger="chatcore.agent"):
        await app.handle(message("@bot hello", mention=True))

    assert provider.calls[0]["model"] == "glm-5"
    assert MODEL_SWITCH_PROMPT in provider.calls[0]["system"]
    assert usage_rows == [("scripted", "glm-5", 3, 2)]
    metrics = next(r.getMessage() for r in caplog.records if "turn metrics" in r.getMessage())
    assert metrics.endswith(" model=glm-5 vision=on")
    assert provider.model == "default-model"


async def test_turn_without_override_uses_the_default_model():
    provider = ModelProvider([Completion(Message.assistant("hi"), "end_turn")])
    app, _switch, _limits, usage_rows = _app(provider)
    await app.handle(message("@bot hello", mention=True))
    assert provider.calls[0]["model"] == "default-model"
    assert usage_rows[0][1] == "default-model"


async def test_unconfigured_switch_leaves_the_prompt_unchanged():
    provider = ModelProvider([Completion(Message.assistant("hi"), "end_turn")])
    app, _switch, _limits, _rows = _app(provider, configured=False)
    await app.handle(message("@bot hello", mention=True))
    assert MODEL_SWITCH_PROMPT not in provider.calls[0]["system"]
    assert "/model" not in provider.calls[0]["system"]


async def test_override_removed_from_the_list_falls_back_to_default():
    provider = ModelProvider([Completion(Message.assistant("hi"), "end_turn")])
    app, switch, _limits, _rows = _app(provider)
    await switch.store.switch(GROUP, "retired-model", set_by="Alice")
    await app.handle(message("@bot hello", mention=True))
    assert provider.calls[0]["model"] == "default-model"


async def test_budget_wrapper_wraps_the_override_provider(monkeypatch):
    import chatcore.agent as agent_module

    wrapped = []
    original = agent_module._BudgetedProvider

    def spy(provider, admit, on_completion):
        wrapped.append(provider.model)
        return original(provider, admit, on_completion)

    monkeypatch.setattr(agent_module, "_BudgetedProvider", spy)
    provider = ModelProvider([Completion(Message.assistant("hi"), "end_turn", Usage(1, 1))])
    app, switch, limits, _rows = _app(provider)
    await switch.store.switch(GROUP, "glm-5", set_by="Alice")
    await app.handle(message("@bot hello", mention=True))
    assert wrapped == ["glm-5"]
    assert limits.recorded == [2]


async def test_text_only_override_drops_turn_start_and_tool_images():
    calls = (ToolCall("read", "read_image", {}),)
    provider = ModelProvider(
        [
            Completion(Message.assistant("", tool_calls=calls), "tool_use"),
            Completion(Message.assistant("I can't see images now."), "end_turn"),
        ]
    )
    app, switch, _limits, _rows = _app(provider)
    seen_vision = []

    async def read_image(ctx):
        seen_vision.append(ctx.turn.vision)
        return ToolResult("image", untrusted=True, images=(ImagePart(JPEG, "image/jpeg"),))

    app.tools.register(Tool("read_image", "read", {"type": "object", "properties": {}}, read_image))
    await switch.store.switch(GROUP, "kimi", set_by="Alice")

    await app.handle(
        message(
            "@bot what is this?",
            mention=True,
            attachments=[Attachment("photo", AttachmentKind.PHOTO)],
        )
    )

    assert [call["model"] for call in provider.calls] == ["kimi", "kimi"]
    assert seen_vision == [False]
    for call in provider.calls:
        assert not any(
            isinstance(part, ImagePart) for item in call["messages"] for part in item.content
        )
    texts = "\n".join(item.text for item in provider.calls[0]["messages"])
    assert "You cannot see them" in texts


async def test_text_only_override_sends_wrapped_description_and_records_both_usages():
    provider = ModelProvider(
        [Completion(Message.assistant("It is a receipt."), "end_turn", Usage(3, 2))]
    )
    vision = ScriptedProvider(
        [Completion(Message.assistant("Cafe receipt total $4.00"), "end_turn", Usage(5, 4))]
    )
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    app, switch, limits, usage_rows = _app(provider, image_reader=reader)
    await switch.store.switch(GROUP, "kimi", set_by="Alice")

    await app.handle(
        message(
            "@bot what is this?",
            mention=True,
            attachments=[Attachment("photo", AttachmentKind.PHOTO)],
        )
    )

    request = provider.calls[0]["messages"]
    assert not any(isinstance(part, ImagePart) for item in request for part in item.content)
    description = next(item.text for item in request if "Cafe receipt total" in item.text)
    assert "vision-model" in description
    assert description.startswith("<untrusted-u")
    assert "</untrusted-u" in description
    assert limits.recorded == [9, 5]
    assert usage_rows == [("scripted", "kimi", 8, 6)]
    assert len(vision.calls) == 1


async def test_describe_cap_and_budget_leave_image_unloaded_but_answer_turn():
    for cap, exhausted, reason in (
        (0, False, "reached today's limit of 0 image descriptions; it resets tomorrow"),
        (30, True, "the bot has used today's token budget"),
    ):
        provider = ModelProvider([Completion(Message.assistant("I cannot view it."), "end_turn")])
        vision = ScriptedProvider([Completion(Message.assistant("must not be used"), "end_turn")])
        reader = ImageReader(vision, model="vision-model", label="vision-model on test")
        app, switch, limits, _rows = _app(
            provider, image_reader=reader, describe_per_user_per_day=cap
        )
        limits.exhausted = exhausted
        await switch.store.switch(GROUP, "kimi", set_by="Alice")
        await app.handle(
            message(
                "@bot what is this?",
                mention=True,
                attachments=[Attachment("photo", AttachmentKind.PHOTO)],
            )
        )
        request_text = "\n".join(item.text for item in provider.calls[0]["messages"])
        assert "none could be described" in request_text
        assert reason in request_text
        assert "only JPEG, PNG or WebP" not in request_text
        assert len(vision.calls) == 0
        assert app.platform.sent


async def test_native_vision_skips_description_reader():
    provider = ModelProvider([Completion(Message.assistant("A photo."), "end_turn")])
    vision = ScriptedProvider([])
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    app, _switch, _limits, _rows = _app(provider, image_reader=reader)
    await app.handle(
        message(
            "@bot what is this?",
            mention=True,
            attachments=[Attachment("photo", AttachmentKind.PHOTO)],
        )
    )
    assert len(vision.calls) == 0
    assert any(
        isinstance(part, ImagePart)
        for item in provider.calls[0]["messages"]
        for part in item.content
    )


async def test_reader_failure_leaves_image_unloaded_but_answers_turn():
    provider = ModelProvider([Completion(Message.assistant("I cannot view it."), "end_turn")])
    vision = ScriptedProvider([ProviderError("private provider error")])
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    app, switch, _limits, _rows = _app(provider, image_reader=reader)
    await switch.store.switch(GROUP, "kimi", set_by="Alice")
    await app.handle(
        message(
            "@bot what is this?",
            mention=True,
            attachments=[Attachment("photo", AttachmentKind.PHOTO)],
        )
    )
    request_text = "\n".join(item.text for item in provider.calls[0]["messages"])
    assert "1 of 1 image attachment(s) couldn't be described because the image reader failed" in (
        request_text
    )
    assert "only JPEG, PNG or WebP" not in request_text
    assert len(vision.calls) == 1
    assert app.platform.sent[-1].text == "I cannot view it."


async def test_failed_descriptions_use_turn_slots_and_record_usage(caplog):
    provider = ModelProvider(
        [
            Completion(
                Message.assistant("", (ToolCall("read-fifth", "read_attachment", {"index": 0}),)),
                "tool_use",
            ),
            Completion(Message.assistant("I cannot view it."), "end_turn"),
        ]
    )
    vision = ScriptedProvider(
        [Completion(Message.assistant("partial"), "max_tokens", Usage(100, 50))] * 4
    )
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    from chatcore_search import SearchPlugin

    app, switch, limits, usage_rows = _app(
        provider,
        vision=False,
        image_reader=reader,
        extra_plugins=[SearchPlugin(FakeEmbedder())],
    )
    await switch.store.switch(GROUP, "kimi", set_by="Alice")
    photos = [Attachment(f"photo-{index}", AttachmentKind.PHOTO) for index in range(4)]
    app.platform.files.update({photo.id: JPEG for photo in photos})
    await app.handle(message("@bot what is this?", mention=True, attachments=photos))
    assert len(vision.calls) == 4
    assert "reason=incomplete" in caplog.text
    assert "partial" not in caplog.text
    tool_result = next(item for item in provider.calls[1]["messages"] if item.role.value == "tool")
    assert tool_result.is_error
    assert tool_result.text == "attachment unavailable: image limit for this turn reached"
    assert limits.recorded.count(150) == 4
    assert usage_rows[0][2:] == (400, 200)


async def test_invalid_turn_start_image_does_not_consume_daily_allowance():
    provider = ModelProvider([Completion(Message.assistant("I cannot view it."), "end_turn")])
    vision = ScriptedProvider([Completion(Message.assistant("unused"), "end_turn")])
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    app, switch, _limits, _rows = _app(provider, vision=False, image_reader=reader)
    await switch.store.switch(GROUP, "kimi", set_by="Alice")
    app.platform.files["bad"] = b"GIF89a"
    increments = []
    original_incr = app.stores.cache.incr

    async def tracked_incr(key, *, ttl_s):
        increments.append(key)
        return await original_incr(key, ttl_s=ttl_s)

    app.stores.cache.incr = tracked_incr
    await app.handle(
        message(
            "@bot what is this?",
            mention=True,
            attachments=[Attachment("bad", AttachmentKind.PHOTO)],
        )
    )
    assert increments == []
    assert not vision.calls


async def test_invalid_images_do_not_use_four_image_load_cap():
    provider = ModelProvider([Completion(Message.assistant("Four images described."), "end_turn")])
    vision = ScriptedProvider(
        [Completion(Message.assistant(f"description {index}"), "end_turn") for index in range(4)]
    )
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    app, _switch, _limits, _rows = _app(provider, vision=False, image_reader=reader)
    app.platform.files["bad"] = b"GIF89a"
    good = [Attachment(f"good-{index}", AttachmentKind.PHOTO) for index in range(4)]
    app.platform.files.update({attachment.id: JPEG for attachment in good})
    await app.handle(
        message(
            "@bot describe these",
            mention=True,
            attachments=[Attachment("bad", AttachmentKind.PHOTO), *good],
        )
    )
    assert len(vision.calls) == 4
    request_text = "\n".join(item.text for item in provider.calls[0]["messages"])
    assert "description 3" in request_text


async def test_describe_deadline_does_not_block_text_turn():
    import asyncio

    class SlowReader(ImageReader):
        async def describe(self, *args, **kwargs):
            await asyncio.sleep(1)
            return await super().describe(*args, **kwargs)

    provider = ModelProvider([Completion(Message.assistant("Still answered."), "end_turn")])
    vision = ScriptedProvider([Completion(Message.assistant("late"), "end_turn")])
    reader = SlowReader(vision, model="vision-model", label="vision-model on test")
    app, switch, _limits, _rows = _app(provider, vision=False, image_reader=reader)
    responder = app.responder
    responder.settings = AgentSettings(
        vision=False, recent_context_messages=0, timeout_s=0.05, final_answer_reserve_s=0.01
    )
    await switch.store.switch(GROUP, "kimi", set_by="Alice")
    import time

    started = time.monotonic()
    await app.handle(
        message(
            "@bot what is this?",
            mention=True,
            attachments=[Attachment("photo", AttachmentKind.PHOTO)],
        )
    )
    assert time.monotonic() - started < 0.5
    assert app.platform.sent[-1].text == "Still answered."


async def test_slow_describe_download_is_bounded_and_turn_answers():
    provider = ModelProvider([Completion(Message.assistant("Still answered."), "end_turn")])
    vision = ScriptedProvider([Completion(Message.assistant("too late"), "end_turn")])
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    app, _switch, _limits, _rows = _app(provider, vision=False, image_reader=reader)
    app.responder.settings = AgentSettings(
        vision=False, recent_context_messages=0, timeout_s=0.12, final_answer_reserve_s=0.03
    )
    original_download = app.platform.download

    async def slow_download(*args, **kwargs):
        await asyncio.sleep(1)
        return await original_download(*args, **kwargs)

    app.platform.download = slow_download
    import time

    started = time.monotonic()
    await app.handle(
        message(
            "@bot what is this?",
            mention=True,
            attachments=[Attachment("photo", AttachmentKind.PHOTO)],
        )
    )
    assert time.monotonic() - started < 0.5
    assert not vision.calls
    assert app.platform.sent[-1].text == "Still answered."


async def test_completed_description_survives_timeout_with_accurate_note():
    class MixedReader(ImageReader):
        async def describe(self, image, **kwargs):
            if image.data.endswith(b"slow"):
                await asyncio.sleep(2)
                return ImageReading("slow description", Usage(), ())
            return ImageReading("fast description", Usage(3, 2), ())

    provider = ModelProvider([Completion(Message.assistant("I saw the fast image."), "end_turn")])
    vision = ScriptedProvider([])
    reader = MixedReader(vision, model="vision-model", label="vision-model on test")
    app, _switch, _limits, _rows = _app(provider, vision=False, image_reader=reader)
    app.responder.settings = AgentSettings(
        vision=False, recent_context_messages=0, timeout_s=1, final_answer_reserve_s=0.5
    )
    app.platform.files.update({"fast": JPEG, "slow": JPEG + b"slow"})
    await app.handle(
        message(
            "@bot compare these",
            mention=True,
            attachments=[
                Attachment("fast", AttachmentKind.PHOTO),
                Attachment("slow", AttachmentKind.PHOTO),
            ],
        )
    )
    request_text = "\n".join(item.text for item in provider.calls[0]["messages"])
    assert "fast description" in request_text
    assert "slow description" not in request_text
    assert "1 of 2 image attachment(s) couldn't be described because" in request_text
    assert "only JPEG, PNG or WebP" not in request_text


async def test_description_usage_triggers_first_completion_budget_check():
    class NearLimit(Limits):
        def __init__(self):
            super().__init__()
            self.total = 99_990

        async def record(self, tokens):
            self.recorded.append(tokens)
            self.total += tokens

        async def budget_exhausted(self):
            return self.total >= 100_000

    provider = ModelProvider([Completion(Message.assistant("Final answer."), "end_turn")])
    vision = ScriptedProvider(
        [Completion(Message.assistant("description"), "end_turn", Usage(100, 50))]
    )
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    app, _switch, _limits, _rows = _app(provider, vision=False, image_reader=reader)
    limits = NearLimit()
    app.responder.limits = limits
    await app.handle(
        message(
            "@bot what is this?",
            mention=True,
            attachments=[Attachment("photo", AttachmentKind.PHOTO)],
        )
    )
    assert limits.total == 100_140
    assert provider.calls[0]["tool_choice"] == "none"
    assert "_FINAL_ANSWER_INSTRUCTION" not in provider.calls[0]["system"]


async def test_slow_context_without_describe_keeps_tools_available():
    provider = ModelProvider([Completion(Message.assistant("Answered."), "end_turn")])
    app, _switch, _limits, _rows = _app(provider, vision=False)
    app.responder.settings = AgentSettings(
        vision=False, recent_context_messages=0, timeout_s=0.03, final_answer_reserve_s=0.01
    )
    original_context = app.responder._context

    async def slow_context(*args, **kwargs):
        await asyncio.sleep(0.06)
        return await original_context(*args, **kwargs)

    app.responder._context = slow_context
    await app.handle(message("@bot hello", mention=True))
    assert provider.calls[0]["tool_choice"] == "auto"
    assert provider.calls[0]["tools"]
    assert "You must now give a final answer" not in provider.calls[0]["system"]


async def test_image_usage_budget_write_survives_turn_deadline():
    class BlockingLimits:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled = False
            self.recorded = []

        async def check(self, *_args):
            return None

        async def record(self, tokens):
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.recorded.append(tokens)

        async def budget_exhausted(self):
            return False

    provider = ModelProvider([Completion(Message.assistant("Answered."), "end_turn")])
    vision = ScriptedProvider(
        [Completion(Message.assistant("description"), "end_turn", Usage(100, 50))]
    )
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    app, _switch, _old_limits, _rows = _app(provider, vision=False, image_reader=reader)
    limits = BlockingLimits()
    app.responder.limits = limits
    app.responder.settings = AgentSettings(
        vision=False, recent_context_messages=0, timeout_s=0.12, final_answer_reserve_s=0.03
    )
    await app.handle(
        message(
            "@bot what is this?",
            mention=True,
            attachments=[Attachment("photo", AttachmentKind.PHOTO)],
        )
    )
    assert limits.started.is_set()
    assert not limits.cancelled
    limits.release.set()
    for _ in range(20):
        if limits.recorded:
            break
        await asyncio.sleep(0.01)
    assert 150 in limits.recorded


async def test_vision_choice_keeps_images():
    provider = ModelProvider([Completion(Message.assistant("a photo"), "end_turn")])
    app, switch, _limits, _rows = _app(provider)
    await switch.store.switch(GROUP, "glm-5", set_by="Alice")
    await app.handle(
        message(
            "@bot what is this?",
            mention=True,
            attachments=[Attachment("photo", AttachmentKind.PHOTO)],
        )
    )
    final_user = provider.calls[0]["messages"][-1]
    assert sum(isinstance(part, ImagePart) for part in final_user.content) == 1


async def test_provider_error_on_override_points_to_model_default():
    class FailingProvider(ModelProvider):
        async def complete(self, messages, **kwargs):
            raise ProviderError("down")

    provider = FailingProvider()
    app, switch, _limits, _rows = _app(provider)
    await switch.store.switch(GROUP, "glm-5", set_by="Alice")
    await app.handle(message("@bot hello", mention=True))
    sent = app.platform.sent[-1].text
    assert "couldn't reach the language model" in sent
    assert "temporary model (glm-5); /model default switches back." in sent


async def test_provider_error_without_override_keeps_the_plain_text():
    class FailingProvider(ModelProvider):
        async def complete(self, messages, **kwargs):
            raise ProviderError("down")

    app, _switch, _limits, _rows = _app(FailingProvider())
    await app.handle(message("@bot hello", mention=True))
    assert "/model" not in app.platform.sent[-1].text


async def test_chatmode_classifier_stays_on_the_default_model():
    provider = ModelProvider([Completion(Message.assistant("NO"), "end_turn")])
    app, switch, _limits, _rows = _app(provider)
    await switch.store.switch(GROUP, "glm-5", set_by="Alice")
    await app.responder.consider(message("anyone free later?"), app)
    assert provider.calls[0]["model"] == "default-model"


async def test_describe_skip_note_names_format_and_per_message_limit():
    provider = ModelProvider([Completion(Message.assistant("Done."), "end_turn")])
    vision = ScriptedProvider(
        [Completion(Message.assistant(f"description {n}"), "end_turn") for n in range(4)]
    )
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    app, _switch, _limits, _rows = _app(provider, vision=False, image_reader=reader)
    names = ["bad", "a", "b", "c", "d", "e"]
    app.platform.files.update({"bad": b"not an image", **{name: JPEG for name in names[1:]}})
    await app.handle(
        message(
            "@bot read these",
            mention=True,
            attachments=[Attachment(name, AttachmentKind.PHOTO) for name in names],
        )
    )
    request_text = "\n".join(item.text for item in provider.calls[0]["messages"])
    assert len(vision.calls) == 4
    assert "1 of 6 image attachment(s) couldn't be loaded (only JPEG, PNG or WebP" in request_text
    assert "1 of 6 image attachment(s) weren't read because only 4 images are read" in request_text
    assert "image reader failed" not in request_text


async def test_description_survives_slow_budget_write_at_deadline():
    class SlowLimits(Limits):
        async def record(self, tokens):
            await asyncio.sleep(0.3)
            self.recorded.append(tokens)

    provider = ModelProvider([Completion(Message.assistant("Saw it."), "end_turn")])
    vision = ScriptedProvider(
        [Completion(Message.assistant("paid description"), "end_turn", Usage(100, 50))]
    )
    reader = ImageReader(vision, model="vision-model", label="vision-model on test")
    app, _switch, _limits, _rows = _app(provider, vision=False, image_reader=reader)
    limits = SlowLimits()
    app.responder.limits = limits
    app.responder.settings = AgentSettings(
        vision=False, recent_context_messages=0, timeout_s=0.3, final_answer_reserve_s=0.2
    )
    await app.handle(
        message(
            "@bot what is this?",
            mention=True,
            attachments=[Attachment("photo", AttachmentKind.PHOTO)],
        )
    )
    request_text = "\n".join(item.text for item in provider.calls[0]["messages"])
    assert "paid description" in request_text
    assert "none could be described" not in request_text
    await asyncio.sleep(0.4)
    assert 150 in limits.recorded


async def test_describe_off_deadline_starts_after_setup(monkeypatch):
    import time

    import chatcore.agent as agent_module

    class SlowMetrics(agent_module.TurnMetrics):
        def __init__(self, *args, **kwargs):
            time.sleep(0.065)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(agent_module, "TurnMetrics", SlowMetrics)
    provider = ModelProvider([Completion(Message.assistant("Answered."), "end_turn")])
    app, _switch, _limits, _rows = _app(provider, vision=False)
    app.responder.settings = AgentSettings(
        vision=False, recent_context_messages=0, timeout_s=0.1, final_answer_reserve_s=0.05
    )
    await app.handle(message("@bot hello", mention=True))
    assert provider.calls[0]["tool_choice"] == "auto"
