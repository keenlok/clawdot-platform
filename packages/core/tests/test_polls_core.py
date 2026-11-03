"""Optional native polls never expand the required platform/plugin contracts."""

import logging

from chatcore import events
from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.plugin import BasePlugin
from chatcore.testing import FakePlatform, memory_stores, user


async def test_poll_answer_dispatch_isolated_and_private(caplog):
    assert hasattr(events, "PollAnswer")
    calls = []

    class Listener(BasePlugin):
        name = "listener"

        async def on_poll_answer(self, answer, stores):
            calls.append(answer)

    class Broken(Listener):
        name = "broken"

        async def on_poll_answer(self, answer, stores):
            raise ValueError("private poll content")

    app = BotApp(
        FakePlatform(),
        stores=memory_stores(),
        access=AccessPolicy([], []),
        plugins=[Broken(), BasePlugin(), Listener()],
        bot_name="bot",
    )
    answer = events.PollAnswer("private-poll-id", user(), ())
    with caplog.at_level(logging.ERROR):
        await app.handle(answer)
    assert calls == [answer]
    assert "broken" in caplog.text and "ValueError" in caplog.text
    assert "private" not in caplog.text


def test_native_polls_are_optional():
    from chatcore.polls import PollPlatform

    assert not isinstance(FakePlatform(), PollPlatform)
