from __future__ import annotations

import importlib
import importlib.util
import io
from importlib import resources

import pytest
from chatcore.llm.types import Completion, Message
from chatcore.testing import FakePlatform, ScriptedProvider, message
from chatcore_console import ConsolePlatform


@pytest.fixture
def scaffold():
    assert importlib.util.find_spec("example_bot") is not None, "scaffold package is not installed"
    return importlib.import_module("example_bot.app")


async def test_console_echo_and_core_commands_need_no_external_services(scaffold):
    async def lines():
        for line in ("@examplebot hello", "/help@examplebot", "/time@examplebot", "::quit"):
            yield line

    output = io.StringIO()
    platform = ConsolePlatform(bot_name="examplebot", lines=lines(), out=output)
    app = scaffold.build_app(env={}, platform=platform)
    await app.run()
    assert "echo: hello" in output.getvalue()
    assert "/time" in output.getvalue() and "/compact" in output.getvalue()
    assert "UTC" in output.getvalue()
    assert app.stores.db is None and app.stores.search is None


async def test_explicit_empty_environment_does_not_enable_ambient_llm(scaffold, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-ambient-value")
    app = scaffold.build_app(env={}, platform=FakePlatform())
    assert type(app.responder).__name__ == "EchoResponder"


async def test_unlisted_chat_is_left_without_a_reply(scaffold):
    platform = FakePlatform()
    app = scaffold.build_app(env={"ALLOWED_CHATS": "fake:permitted"}, platform=platform)
    incoming = message("@examplebot hello", mention=True)
    await app.handle(incoming)
    assert platform.left == [incoming.chat]
    assert platform.texts == []


async def test_configured_provider_uses_packaged_generic_persona(scaffold):
    platform = FakePlatform()
    provider = ScriptedProvider(
        [Completion(Message.assistant("Hello from the example."), "end_turn")]
    )
    app = scaffold.build_app(
        env={"ALLOWED_CHATS": "fake:group"}, platform=platform, provider=provider
    )
    await app.handle(message("@examplebot hello", mention=True))
    persona = (
        resources.files("example_bot.prompts").joinpath("system.md").read_text("utf-8").strip()
    )
    assert persona in provider.calls[0]["system"]
    assert "helpful assistant" in persona
    assert platform.texts[-1] == "Hello from the example."


def test_requested_llm_requires_a_credential(scaffold):
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        scaffold.build_app(env={"LLM_PROVIDER": "openai_compat"})


def test_scaffold_package_is_installed():
    assert importlib.util.find_spec("example_bot") is not None, "scaffold package is not installed"
