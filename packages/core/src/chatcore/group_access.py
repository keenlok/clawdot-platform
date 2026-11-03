"""Optional live group-role capability; deliberately separate from global admins."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from chatcore.events import ChatRef, UserRef


@runtime_checkable
class GroupAdminPlatform(Protocol):
    async def is_group_admin(self, chat: ChatRef, user: UserRef) -> bool:
        """Return the user's current group admin role, failing closed on lookup errors."""
