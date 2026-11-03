"""Minimal app wiring: console, in-memory stores and two core plugins."""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib import resources

from chatcore.access import AccessPolicy
from chatcore.agent import AgentResponder, AgentSettings
from chatcore.app import BotApp
from chatcore.conversation import ConversationPlugin
from chatcore.display_time_plugin import DisplayTimePlugin
from chatcore.limits import Limits
from chatcore.llm.base import Provider
from chatcore.llm.factory import LLMSettings, build_provider
from chatcore.platform import ChatPlatform
from chatcore.store.base import Stores
from chatcore.store.memory import MemoryCache, MemoryCoreRepo
from chatcore_console import ConsolePlatform


def build_app(
    *,
    env: Mapping[str, str] | None = None,
    platform: ChatPlatform | None = None,
    provider: Provider | None = None,
) -> BotApp:
    """Build a local example; omitted service settings never create remote stores."""
    env = os.environ if env is None else env
    bot_name = env.get("BOT_NAME") or "examplebot"
    platform = platform or ConsolePlatform(bot_name=bot_name)
    stores = Stores(repo=MemoryCoreRepo(), cache=MemoryCache(prefix="example:"))
    access = AccessPolicy(
        [key.strip() for key in env.get("ALLOWED_CHATS", "console:group").split(",")],
        [key.strip() for key in env.get("ADMIN_USERS", "console:me").split(",")],
    )
    if provider is None and env.get("LLM_PROVIDER"):
        # Supplying every setting keeps unrelated process environment out of an explicit mapping.
        settings = LLMSettings(
            **{
                name: env.get(name) or field.default
                for name, field in LLMSettings.model_fields.items()
            }
        )
        provider = build_provider(settings)
    responder = None
    if provider is not None:
        persona = resources.files("example_bot.prompts").joinpath("system.md").read_text("utf-8")
        responder = AgentResponder(
            provider,
            system_prompt=persona,
            settings=AgentSettings(),
            limits=Limits(stores.cache),
        )
    return BotApp(
        platform,
        stores=stores,
        access=access,
        plugins=[DisplayTimePlugin(), ConversationPlugin(enable_chatmode=False)],
        responder=responder,
        bot_name=bot_name,
        group_mentions_only=True,
    )
