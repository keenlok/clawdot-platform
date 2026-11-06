from chatcore.access import AccessPolicy
from chatcore.app import BotApp
from chatcore.testing import FakePlatform, memory_stores, message
from chatcore.threads import TurnOrigin, current_turn_origin


def test_turn_origin_context_defaults_and_restores():
    assert current_turn_origin.get() is None


async def test_bot_app_sets_turn_origin_during_responder_and_resets_it():
    class CaptureResponder:
        origin = None

        async def respond(self, _message, _text, _app):
            self.origin = current_turn_origin.get()

    responder = CaptureResponder()
    app = BotApp(
        FakePlatform(),
        stores=memory_stores(),
        access=AccessPolicy([], []),
        responder=responder,
        bot_name="testbot",
    )
    origin = TurnOrigin("bare_tag", "stored-42")
    await app._respond_locked(message("hello"), "earlier human message", origin=origin)
    assert responder.origin == origin
    assert current_turn_origin.get() is None
    token = current_turn_origin.set(TurnOrigin("bare_tag", "stored-42"))
    try:
        assert current_turn_origin.get() == TurnOrigin("bare_tag", "stored-42")
    finally:
        current_turn_origin.reset(token)
    assert current_turn_origin.get() is None
