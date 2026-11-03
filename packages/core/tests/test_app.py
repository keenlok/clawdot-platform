from dataclasses import replace
from datetime import UTC, datetime

import pytest
from chatcore.access import AccessPolicy
from chatcore.administration.store import AccessSnapshot, Restriction
from chatcore.app import BotApp
from chatcore.events import BotAddedToChat, ButtonPress, ChatRef, MemberLeft, MessageRef
from chatcore.pending import PendingAction
from chatcore.platform import Capabilities
from chatcore.plugin import BasePlugin, Command, CommandContext
from chatcore.testing import GROUP, PLATFORM, FakePlatform, memory_stores, message, press, user

ALICE, BOB, ADMIN = user("alice"), user("bob"), user("root")


class Committer:
    def __init__(self) -> None:
        self.committed: list[PendingAction] = []

    async def commit(self, action: PendingAction, ctx: CommandContext) -> str:
        self.committed.append(action)
        return f"saved {action.payload['amount']}"


class LedgerPlugin(BasePlugin):
    name = "ledger"

    def __init__(self) -> None:
        self.committer = Committer()

    def commands(self) -> list[Command]:
        async def secret(ctx: CommandContext, args: str) -> str:
            return "admin stuff"

        return [Command("purge", "admin only", secret, admin_only=True)]

    def action_handlers(self):
        return {"add": self.committer}


def make_app(
    platform: FakePlatform | None = None, *, group_mentions_only: bool = False
) -> tuple[BotApp, FakePlatform, LedgerPlugin]:
    platform = platform or FakePlatform()
    platform.members[GROUP.key] = {ALICE.ref.key, BOB.ref.key}
    plugin = LedgerPlugin()
    app = BotApp(
        platform,
        stores=memory_stores(),
        access=AccessPolicy([GROUP.key], [ADMIN.ref.key]),
        plugins=[plugin],
        bot_name="sbbot",
        group_mentions_only=group_mentions_only,
    )
    return app, platform, plugin


async def test_mention_in_allowed_group_gets_echo_and_is_logged():
    app, platform, _ = make_app()
    await app.handle(message("@sbbot hello there", mention=True))
    assert platform.texts == ["echo: hello there"]
    logged = await app.stores.repo.recent_messages(GROUP, limit=10)
    assert [m.is_bot for m in logged] == [False, True]


async def test_unaddressed_group_message_is_logged_but_not_answered():
    app, platform, _ = make_app()
    await app.handle(message("just chatting"))
    assert platform.texts == []
    assert len(await app.stores.repo.recent_messages(GROUP, limit=10)) == 1


async def test_bots_never_start_turns():
    app, platform, _ = make_app()
    await app.handle(message("@sbbot hi", mention=True, sender=user("otherbot", is_bot=True)))
    assert platform.texts == []


async def test_reply_to_bot_and_ask_command_respond():
    app, platform, _ = make_app()
    await app.handle(message("and then?", reply_to_bot=True))
    await app.handle(message("/ask what time"))
    assert platform.texts == ["echo: and then?", "echo: what time"]


async def test_non_allowlisted_group_is_left_and_not_logged():
    app, platform, _ = make_app()
    other = ChatRef(PLATFORM, "strangers")
    await app.handle(message("@sbbot hi", mention=True, chat=other))
    await app.handle(BotAddedToChat(other))
    assert platform.left == [other, other]
    assert platform.texts == []
    assert await app.stores.repo.recent_messages(other, limit=10) == []


async def test_dm_only_from_current_members():
    app, platform, _ = make_app()
    await app.handle(message("hi", dm=True, sender=ALICE))
    await app.handle(message("hi", dm=True, sender=user("stranger")))
    assert platform.texts == ["echo: hi"]


async def test_member_who_left_loses_dm_access():
    app, platform, _ = make_app()
    await app.handle(message("hi", dm=True, sender=BOB))
    platform.members[GROUP.key].discard(BOB.ref.key)
    await app.handle(MemberLeft(GROUP, BOB))
    await app.handle(message("hi again", dm=True, sender=BOB))
    assert platform.texts == ["echo: hi"]


async def test_admin_commands_are_silent_for_non_admins():
    app, platform, _ = make_app()
    await app.handle(message("/purge", sender=ALICE))
    await app.handle(message("/purge", sender=ADMIN))
    assert platform.texts == ["admin stuff"]


async def test_commands_for_other_bots_are_ignored():
    app, platform, _ = make_app()
    await app.handle(message("/help@someotherbot"))
    await app.handle(message("/help@sbbot"))
    assert len(platform.texts) == 1 and "/help" in platform.texts[0]
    assert "/purge" not in platform.texts[0]  # hidden from non-admins


def pending(requester=ALICE, chat=GROUP) -> PendingAction:
    return PendingAction.create(
        chat=chat,
        requester=requester.ref,
        plugin="ledger",
        kind="add",
        payload={"amount": 120},
        summary="Add $120 dinner, paid by Alice",
    )


@pytest.mark.parametrize("group_mentions_only", [False, True])
async def test_confirm_buttons_only_work_for_requester_and_only_once(group_mentions_only):
    app, platform, plugin = make_app(group_mentions_only=group_mentions_only)
    action = pending()
    await app.confirm.request(action, ALICE)
    assert platform.sent[-1].action_id == action.id

    await app.handle(press(action.id, sender=BOB))  # not the requester: ignored silently
    assert plugin.committer.committed == []

    await app.handle(press(action.id, sender=ALICE))
    await app.handle(press(action.id, sender=ALICE))  # second press: already used
    assert len(plugin.committer.committed) == 1
    assert platform.texts[-2:] == ["saved 120", platform.texts[-1]]
    assert "expired or was already handled" in platform.texts[-1]


async def test_cancel_consumes_the_action():
    app, platform, plugin = make_app()
    action = pending()
    await app.confirm.request(action, ALICE)
    await app.handle(press(action.id, sender=ALICE, confirm=False))
    await app.handle(press(action.id, sender=ALICE))
    assert plugin.committer.committed == []
    assert platform.texts[-2].startswith("Cancelled")


async def test_expired_action_cannot_be_confirmed():
    app, platform, plugin = make_app()
    action = pending()
    action = replace(action, expires_at=action.created_at)
    await app.confirm.request(action, ALICE)
    await app.handle(press(action.id, sender=ALICE))
    assert plugin.committer.committed == []


async def test_button_from_another_chat_is_ignored():
    app, platform, plugin = make_app()
    action = pending()
    await app.confirm.request(action, ALICE)
    await app.handle(press(action.id, sender=ALICE, chat=ChatRef(PLATFORM, "elsewhere")))
    assert plugin.committer.committed == []


async def test_confirm_press_uses_action_thread_for_command_and_reply():
    app, platform, _plugin = make_app()
    action = replace(pending(), thread_id="action-topic")
    await app.confirm.request(action, ALICE)
    press_message = MessageRef(GROUP, "button-msg", "button-topic")
    await app.handle(ButtonPress(GROUP, ALICE, action.id, "confirm", press_message))
    assert platform.sent[-1].ref.thread_id == "action-topic"


async def test_restricted_confirm_notice_stays_in_action_thread():
    app, platform, _plugin = make_app()
    app.access._snapshot = AccessSnapshot(
        1,
        {},
        frozenset(),
        restrictions={
            (ALICE.ref.key, GROUP.key): Restriction(
                ALICE.ref.key, GROUP.key, "block", None, ADMIN.ref.key, datetime.now(UTC)
            )
        },
    )
    action = replace(pending(), thread_id="restricted-topic")
    await app.confirm.request(action, ALICE)
    await app.handle(ButtonPress(GROUP, ALICE, action.id, "confirm"))
    assert platform.sent[-1].text == "You can't use this bot here right now."
    assert platform.sent[-1].ref.thread_id == "restricted-topic"


async def test_reply_code_result_uses_typed_message_thread():
    platform = FakePlatform(
        capabilities=Capabilities(
            buttons=False, edit=False, threads=True, files=False, dm=True, max_message_len=1000
        )
    )
    app, platform, _plugin = make_app(platform)
    action = pending()
    await app.confirm.request(action, ALICE)
    await app.handle(message(f"YES {action.reply_code}", sender=ALICE, thread_id="typed-topic"))
    assert platform.sent[-1].ref.thread_id == "typed-topic"


@pytest.mark.parametrize("group_mentions_only", [False, True])
async def test_reply_code_fallback_without_buttons(group_mentions_only):
    platform = FakePlatform(
        capabilities=Capabilities(
            buttons=False, edit=False, threads=False, files=False, dm=True, max_message_len=1000
        )
    )
    app, platform, plugin = make_app(platform, group_mentions_only=group_mentions_only)
    action = pending()
    await app.confirm.request(action, ALICE)
    assert f"YES {action.reply_code}" in platform.texts[-1]

    await app.handle(message(f"yes {action.reply_code}", sender=BOB))  # wrong user
    assert plugin.committer.committed == []
    await app.handle(message(f"YES {action.reply_code}", sender=ALICE))
    assert len(plugin.committer.committed) == 1


async def test_unknown_pending_kind_is_rejected_up_front():
    app, _, _ = make_app()
    bad = PendingAction.create(
        chat=GROUP, requester=ALICE.ref, plugin="ledger", kind="nope", payload={}, summary="x"
    )
    with pytest.raises(LookupError):
        await app.confirm.request(bad, ALICE)


async def test_responder_errors_reply_generically_without_crashing():
    class Broken:
        async def respond(self, message, text, app):
            raise RuntimeError("secret detail sk-should-not-leak")

        async def consider(self, message, app):
            return None

    app, platform, _ = make_app()
    app.responder = Broken()
    await app.handle(message("@sbbot hi", mention=True))
    assert platform.texts == ["Sorry, something went wrong. Please try again."]


async def test_help_opens_with_plain_words_hint_and_groups_by_plugin():
    app, platform, plugin = make_app(group_mentions_only=True)
    assert app.command_groups["help"] == "General"
    assert app.command_groups["purge"] == (plugin.help_title or plugin.name.capitalize())
    await app.handle(message("/help@sbbot"))
    text = platform.texts[-1]
    blocks = text.split("\n\n")
    assert blocks[0].startswith("You can also just ask me in plain words")
    assert blocks[1].startswith("General:\n/help@sbbot - ")
    assert "/purge" not in text
    await app.handle(message("/help", dm=True, sender=ADMIN))
    assert "/purge - " in platform.texts[-1]
