from __future__ import annotations

from collections.abc import Awaitable, Callable

from chatcore.events import ChatRef, UserRef

GroupAdminCheck = Callable[[ChatRef, UserRef], Awaitable[bool]]
ScopeAllowed = Callable[[ChatRef], bool]


class ExtensionAuthority:
    def __init__(self, scope_allowed: ScopeAllowed, group_admin: GroupAdminCheck | None):
        self.scope_allowed = scope_allowed
        self.group_admin = group_admin

    async def may_manage(self, *, chat: ChatRef, user: UserRef, is_dm: bool) -> bool:
        try:
            if is_dm:
                return chat.key == user.key
            if not self.scope_allowed(chat):
                return False
            if self.group_admin is None:
                return False
            return bool(await self.group_admin(chat, user))
        except Exception:
            return False
