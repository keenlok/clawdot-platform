"""Who the bot talks to (§6a least privilege): allowlisted groups, DMs only from current members of
those groups, and admin commands only for listed users."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from chatcore.administration.store import Restriction, is_global_scope
from chatcore.events import ChatRef, UserRef
from chatcore.limits import BudgetOverride
from chatcore.platform import ChatPlatform, PlatformError
from chatcore.store.base import Cache

if TYPE_CHECKING:
    from chatcore.administration.store import AccessSnapshot, AdministrationStore

log = logging.getLogger(__name__)

MEMBERSHIP_TTL_S = 3600


class AccessPolicy:
    def __init__(
        self,
        allowed_chats: Iterable[str],
        admin_users: Iterable[str],
        *,
        owners: Iterable[str] = (),
        store: AdministrationStore | None = None,
    ) -> None:
        bootstrap_chats = set()
        invalid_count = 0
        for key in allowed_chats:
            try:
                chat = ChatRef.parse(key)
            except (TypeError, ValueError):
                invalid_count += 1
                continue
            if not chat.id or chat.id == "*" or ":" in chat.id:
                invalid_count += 1
                continue
            bootstrap_chats.add(chat.key)
        if invalid_count:
            log.warning("ignored invalid bootstrap chats count=%d", invalid_count)
        self.bootstrap_chats = frozenset(bootstrap_chats)
        self.admin_users = frozenset(admin_users)
        self.owners = frozenset(owners)
        self.store = store
        self._snapshot: AccessSnapshot | None = None

    @property
    def allowed_chats(self) -> frozenset[str]:
        overrides = self._snapshot.overrides if self._snapshot else {}
        allowed = set()
        for key in self.bootstrap_chats | overrides.keys():
            try:
                chat = ChatRef.parse(key)
            except (TypeError, ValueError):
                continue
            if is_global_scope(chat):
                continue
            if overrides.get(key, key in self.bootstrap_chats):
                allowed.add(key)
        return frozenset(allowed)

    async def refresh(self) -> None:
        """Replace all dynamic permissions in one operation; store errors fail closed upstream."""
        if self.store is not None:
            self._snapshot = await self.store.snapshot()

    def is_chat_allowed(self, chat: ChatRef) -> bool:
        return chat.key in self.allowed_chats

    def is_admin(self, user: UserRef) -> bool:
        return user.key in self.admin_users or self.is_owner(user)

    def is_owner(self, user: UserRef) -> bool:
        return user.key in self.owners

    def can_setup(self, user: UserRef, group: ChatRef) -> bool:
        return self.is_owner(user) or bool(
            self._snapshot and (user.key, group.key) in self._snapshot.assignments
        )

    def assigned_groups(self, user: UserRef) -> frozenset[str]:
        if self._snapshot is None:
            return frozenset()
        return frozenset(
            group for subject, group in self._snapshot.assignments if subject == user.key
        )

    def can_moderate(self, user: UserRef, group: ChatRef) -> bool:
        return self.is_owner(user) or bool(
            self._snapshot and (user.key, group.key) in self._snapshot.moderators
        )

    def moderated_groups(self, user: UserRef) -> frozenset[str]:
        if self._snapshot is None:
            return frozenset()
        return frozenset(
            group for subject, group in self._snapshot.moderators if subject == user.key
        )

    def restriction_for(
        self,
        user: UserRef,
        chat: ChatRef,
        *,
        is_dm: bool,
        now: datetime | None = None,
    ) -> Restriction | None:
        if self.is_owner(user) or self._snapshot is None:
            return None
        instant = now or datetime.now(UTC)
        applicable = []
        for (user_key, scope), restriction in self._snapshot.restrictions.items():
            if user_key != user.key:
                continue
            scoped = ChatRef.parse(scope)
            if scoped.platform != user.platform or chat.platform != user.platform:
                continue
            if is_global_scope(scoped) or (not is_dm and chat.key == scope):
                if restriction.kind == "pause" and restriction.until <= instant:
                    continue
                applicable.append(restriction)
        blocks = [row for row in applicable if row.kind == "block"]
        if blocks:
            return blocks[0]
        pauses = [row for row in applicable if row.kind == "pause"]
        return max(pauses, key=lambda row: row.until) if pauses else None

    def user_limit(self, user: UserRef) -> int | None:
        return self._snapshot.user_limits.get(user.key) if self._snapshot else None

    def budget_override(self) -> BudgetOverride | None:
        return self._snapshot.budget if self._snapshot else None

    @property
    def snapshot(self) -> AccessSnapshot | None:
        return self._snapshot

    def groups_on(self, platform: str) -> list[ChatRef]:
        groups = []
        for key in sorted(self.allowed_chats):
            if not key.startswith(f"{platform}:") or _is_positive_tg_user(key):
                continue
            chat = ChatRef.parse(key)
            if not is_global_scope(chat):
                groups.append(chat)
        return groups

    async def member_groups(
        self, user: UserRef, platform: ChatPlatform, cache: Cache
    ) -> list[ChatRef]:
        """Allowlisted groups ``user`` currently belongs to. Cached for an hour per group."""
        groups: list[ChatRef] = []
        for chat in self.groups_on(platform.name):
            key = f"member:{chat.key}:{user.key}"
            cached = await cache.get(key)
            if cached is None:
                try:
                    member = await platform.is_member(chat, user)
                except PlatformError:
                    log.warning("membership check failed for %s", chat.key)
                    continue
                await cache.set(key, b"1" if member else b"0", ttl_s=MEMBERSHIP_TTL_S)
            else:
                member = cached == b"1"
            if member:
                groups.append(chat)
        return groups

    async def can_dm(self, user: UserRef, platform: ChatPlatform, cache: Cache) -> bool:
        return (
            self.is_admin(user)
            or bool(self.assigned_groups(user))
            or bool(self.moderated_groups(user))
            or bool(await self.member_groups(user, platform, cache))
        )


def _is_positive_tg_user(key: str) -> bool:
    platform, _, ident = key.partition(":")
    return platform == "tg" and ident.isascii() and ident.isdecimal() and int(ident) > 0
