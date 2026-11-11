from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import openai
import pytest
from chatcore_image.service import MAX_IMAGE_BYTES, ImageError, ImageService
from chatcore_image.settings import ImageSettings
from pydantic import SecretStr, ValidationError

PNG = b"\x89PNG\r\n\x1a\nimage"


def settings(**kwargs):
    return ImageSettings(
        IMAGE_ENABLED=True,
        IMAGE_OPENAI_API_KEY=SecretStr("fake-key"),
        **kwargs,
    )


def client_for(data=PNG, *, error=None):
    client = SimpleNamespace(images=SimpleNamespace(generate=AsyncMock()))
    if error:
        client.images.generate.side_effect = error
    else:
        client.images.generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(data).decode())]
        )
    return client


@pytest.mark.asyncio
async def test_request_shape_and_png_decoding():
    client = client_for()
    service = ImageService(settings(), client=client)
    assert await service.generate("a landscape", "landscape", user_key="u") == PNG
    client.images.generate.assert_awaited_once_with(
        model="gpt-image-2.5-flare",
        prompt="a landscape",
        size="1536x1024",
        quality="medium",
        n=1,
        output_format="png",
        moderation="auto",
    )


async def test_real_client_is_built_lazily_and_closed():
    service = ImageService(settings())
    assert service._client is None
    client = service._get_client()
    assert isinstance(client, openai.AsyncOpenAI)
    assert client.max_retries == 0
    await service.aclose()


@pytest.mark.parametrize("data", [b"html", b"", b"not png"])
async def test_rejects_invalid_png(data):
    with pytest.raises(ImageError, match="invalid_output"):
        await ImageService(settings(), client=client_for(data)).generate(
            "safe", "square", user_key="u"
        )


async def test_rejects_invalid_base64_and_oversize_output():
    client = SimpleNamespace(
        images=SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(b64_json="!!!")]))
        )
    )
    with pytest.raises(ImageError, match="invalid_output"):
        await ImageService(settings(), client=client).generate("safe", "square", user_key="u")
    with pytest.raises(ImageError, match="invalid_output"):
        await ImageService(
            settings(), client=client_for(b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES)
        ).generate("safe", "square", user_key="u")


async def test_daily_and_user_quota_rollover():
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    service = ImageService(
        settings(IMAGE_MAX_PER_USER_DAY=1, IMAGE_MAX_PER_DAY=2),
        client=client_for(),
        clock=lambda: now[0],
    )
    await service.generate("first", "square", user_key="u")
    with pytest.raises(ImageError, match="quota"):
        await service.generate("second", "square", user_key="u")
    assert service._client.images.generate.await_count == 1
    await service.generate("other", "square", user_key="v")
    now[0] += timedelta(days=1)
    await service.generate("new day", "square", user_key="u")


async def test_busy_does_not_charge_and_failed_attempt_does():
    gate = asyncio.Event()
    client = client_for()

    async def blocked_generate(**_):
        return await wait_for_response(gate)

    client.images.generate.side_effect = blocked_generate
    service = ImageService(settings(IMAGE_MAX_PER_DAY=20, IMAGE_MAX_PER_USER_DAY=2), client=client)
    first = asyncio.create_task(service.generate("one", "square", user_key="a"))
    second = asyncio.create_task(service.generate("two", "square", user_key="b"))
    await asyncio.sleep(0)
    with pytest.raises(ImageError, match="busy"):
        await service.generate("third", "square", user_key="c")
    assert client.images.generate.await_count == 2
    gate.set()
    await asyncio.gather(first, second)
    await service.generate("third", "square", user_key="c")
    await service.generate("fourth", "square", user_key="c")
    with pytest.raises(ImageError, match="quota"):
        await service.generate("fifth", "square", user_key="c")
    assert client.images.generate.await_count == 4

    failed = ImageService(settings(IMAGE_MAX_PER_DAY=1), client=client_for(error=TimeoutError()))
    with pytest.raises(ImageError, match="unavailable"):
        await failed.generate("prompt-secret", "square", user_key="u")
    with pytest.raises(ImageError, match="quota"):
        await failed.generate("again", "square", user_key="u")


async def wait_for_response(event):
    await event.wait()
    return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(PNG).decode())])


@pytest.mark.parametrize("status", [400, 500])
async def test_provider_http_failures_are_safe(status):
    error = openai.APIStatusError(
        "private response text",
        response=SimpleNamespace(status_code=status, request=object(), headers={}),
        body={"code": "invalid"},
    )
    with pytest.raises(ImageError, match="unavailable"):
        await ImageService(settings(), client=client_for(error=error)).generate(
            "secret prompt", "square", user_key="u"
        )


@pytest.mark.parametrize(
    ("code", "body", "expected"),
    [
        ("content_policy_violation", {"message": "moderation blocked"}, "refused"),
        ("moderation_blocked", {"message": "policy rejected"}, "refused"),
        ("content_policy_violation", {"message": "policy rejected"}, "refused"),
        (None, {"message": "moderation blocked"}, "refused"),
        ("invalid_request", {"message": "bad request"}, "unavailable"),
    ],
)
async def test_bad_request_error_classification(code, body, expected):
    error = openai.BadRequestError(
        "private",
        response=SimpleNamespace(status_code=400, request=object(), headers={}),
        body=body,
    )
    error.code = code
    with pytest.raises(ImageError, match=expected):
        await ImageService(settings(), client=client_for(error=error)).generate(
            "safe", "square", user_key="u"
        )


async def test_daily_quota_refuses_other_users_without_provider_call():
    client = client_for()
    service = ImageService(settings(IMAGE_MAX_PER_DAY=1), client=client)
    await service.generate("first", "square", user_key="user-a")
    with pytest.raises(ImageError, match="quota"):
        await service.generate("second", "square", user_key="user-b")
    assert client.images.generate.await_count == 1


async def test_logs_never_include_prompts_or_keys(caplog):
    service = ImageService(settings(), client=client_for(error=TimeoutError()))
    with pytest.raises(ImageError):
        await service.generate("private prompt text", "square", user_key="u")
    assert "private prompt text" not in caplog.text
    assert "fake-key" not in caplog.text


def test_availability_and_settings_validation():
    assert not ImageService(ImageSettings()).available
    assert not ImageService(ImageSettings(IMAGE_ENABLED=True)).available
    assert not ImageService(settings(IMAGE_MAX_PER_DAY=0)).available
    with pytest.raises(ValidationError):
        ImageSettings(IMAGE_MODEL="unknown")
    with pytest.raises(ValidationError):
        ImageSettings(IMAGE_OPENAI_BASE_URL="http://example.com")
    with pytest.raises(ValidationError):
        ImageSettings(IMAGE_MAX_PER_DAY=10001)
