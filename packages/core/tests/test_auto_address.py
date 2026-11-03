from datetime import UTC, datetime

import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.store import AccessSnapshot, Restriction
from chatcore.app import BotApp
from chatcore.plugin import BasePlugin
from chatcore.testing import GROUP, FakePlatform, memory_stores, message, user


class AutoAddressPlugin(BasePlugin):
    name = "auto_address_test"

    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def auto_address(self, incoming, stores):
        self.calls.append(incoming)
        if self.error:
            raise self.error
        return self.result


def make_app(plugin, *, restricted=False):
    platform = FakePlatform()
    platform.members[GROUP.key] = {user("alice").ref.key}
    access = AccessPolicy([GROUP.key], [])
    if restricted:
        access._snapshot = AccessSnapshot(
            1,
            {},
            frozenset(),
            restrictions={
                (user("alice").ref.key, GROUP.key): Restriction(
                    user("alice").ref.key,
                    GROUP.key,
                    "block",
                    None,
                    user("root").ref.key,
                    datetime.now(UTC),
                )
            },
        )
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=access,
        plugins=[plugin],
        bot_name="sbbot",
        group_mentions_only=True,
    )
    return app, platform


@pytest.mark.asyncio
async def test_auto_addressed_untagged_group_message_gets_a_response():
    plugin = AutoAddressPlugin()
    app, platform = make_app(plugin)

    await app.handle(message("a normal group message"))

    assert platform.texts == ["echo: a normal group message"]
    assert len(plugin.calls) == 1


@pytest.mark.asyncio
async def test_false_or_failing_auto_address_hook_leaves_message_ignored(caplog):
    false_plugin = AutoAddressPlugin(result=False)
    app, platform = make_app(false_plugin)
    await app.handle(message("just chatting"))
    assert platform.texts == [] and len(false_plugin.calls) == 1

    failed_plugin = AutoAddressPlugin(error=RuntimeError("private message text"))
    failed_app, failed_platform = make_app(failed_plugin)
    await failed_app.handle(message("another chat"))
    assert failed_platform.texts == [] and len(failed_plugin.calls) == 1
    assert "RuntimeError" in caplog.text
    assert "private message text" not in caplog.text


@pytest.mark.asyncio
async def test_hook_is_not_consulted_for_bots_empty_messages_commands_or_other_addressing():
    plugin = AutoAddressPlugin()
    app, platform = make_app(plugin)
    await app.handle(message("bot says hello", sender=user("bot", is_bot=True)))
    await app.handle(message("   "))
    await app.handle(message("/polls"))
    await app.handle(message("tagged question", mention=True))
    await app.handle(message("direct question", dm=True))

    assert len(plugin.calls) == 0
    assert platform.texts == ["echo: tagged question", "echo: direct question"]


@pytest.mark.asyncio
async def test_restricted_auto_addressed_message_is_silent():
    plugin = AutoAddressPlugin()
    app, platform = make_app(plugin, restricted=True)

    await app.handle(message("restricted untagged message"))

    assert len(plugin.calls) == 1
    assert platform.texts == []
