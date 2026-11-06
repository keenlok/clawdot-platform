"""Read trusted human authorship for a chat message from the canonical log."""

from __future__ import annotations

from typing import Any

from chatcore.events import ChatRef


async def human_source(db: Any, chat: ChatRef, message_id: str) -> tuple[str, bool] | None:
    """Return ``(author_key, is_bot)`` for a source in this chat, if it still exists."""
    lookup = getattr(db, "human_source", None)
    if callable(lookup):
        return await lookup(chat, message_id)
    row = await db.fetchrow(
        "SELECT user_key, is_bot FROM messages WHERE chat_key=$1 AND message_id=$2",
        chat.key,
        message_id,
    )
    return (str(row["user_key"]), bool(row["is_bot"])) if row else None
