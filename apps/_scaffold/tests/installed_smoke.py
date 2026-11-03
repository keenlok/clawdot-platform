"""Run from outside the repository with an isolated, wheel-only Python environment."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from importlib import resources
from pathlib import Path

import chatcore
import chatcore_console
import example_bot
from chatcore.explainers import ExplainerCatalog
from chatcore.llm.types import Completion, Message
from chatcore.testing import FakePlatform, ScriptedProvider, message
from example_bot.app import build_app


async def _agent_smoke() -> None:
    provider = ScriptedProvider(
        [Completion(Message.assistant("Installed example works."), "end_turn")]
    )
    platform = FakePlatform()
    app = build_app(env={"ALLOWED_CHATS": "fake:group"}, provider=provider, platform=platform)
    await app.handle(message("@examplebot hello", mention=True))
    persona = (
        resources.files("example_bot.prompts").joinpath("system.md").read_text("utf-8").strip()
    )
    assert persona in provider.calls[0]["system"]
    assert platform.texts == ["Installed example works."]


def main() -> None:
    prefix = Path(sys.prefix).resolve()
    for module in (chatcore, chatcore_console, example_bot):
        assert Path(module.__file__).resolve().is_relative_to(prefix), module.__name__
    asyncio.run(_agent_smoke())
    assert "Postgres" in ExplainerCatalog.from_plugins([]).section("framework", "storage")
    result = subprocess.run(
        [str(Path(sys.executable).with_name("example-bot"))],
        input="@examplebot packaged hello\n/help@examplebot\n/explain@examplebot\n::quit\n",
        env={"BOT_NAME": "examplebot", "ALLOWED_CHATS": "console:group"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "echo: packaged hello" in result.stdout
    assert "/time@examplebot" in result.stdout
    assert "How the bot works" in result.stdout
    assert "sha256:" in result.stdout
    print(
        "Installed scaffold: wheel imports, persona, agent wiring and console entry point passed."
    )


if __name__ == "__main__":
    main()
