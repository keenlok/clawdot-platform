from chatcore.events import ChatRef
from chatcore.store.evidence import human_source
from chatcore.store.memory import MemoryCoreRepo
from chatcore.testing import message, user


async def test_human_source_is_chat_scoped_and_excludes_bots():
    repo = MemoryCoreRepo()
    chat = ChatRef("fake", "evidence")
    alice = user("alice")
    human = message("source", chat=chat, sender=alice)
    await repo.log_message(human)
    await repo.log_bot_message(chat, "bot-1", "reply", thread_id=None)

    assert await repo.human_source(chat, human.message_id) == (alice.ref.key, False)
    assert await repo.human_source(chat, "bot-1") == ("fake:bot", True)
    assert await repo.human_source(ChatRef("fake", "elsewhere"), human.message_id) is None
    assert await human_source(repo, chat, human.message_id) == (
        alice.ref.key,
        False,
    )
