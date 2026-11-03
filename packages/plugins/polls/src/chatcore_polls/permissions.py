"""Live group management checks; global bot administration alone is insufficient."""

import logging

from chatcore.access import AccessPolicy
from chatcore.events import UserRef
from chatcore.group_access import GroupAdminPlatform
from chatcore.platform import ChatPlatform

from chatcore_polls.models import Scope

log = logging.getLogger(__name__)


async def is_group_manager(
    access: AccessPolicy,
    platform: ChatPlatform,
    scope: Scope,
    user: UserRef,
) -> bool:
    if scope.is_dm or user.platform != scope.chat.platform or platform.name != scope.chat.platform:
        return False
    refreshed = True
    try:
        await access.refresh()
    except Exception as exc:
        refreshed = False
        log.warning("poll manager access refresh failed: %s", type(exc).__name__)
    if refreshed and access.can_setup(user, scope.chat):
        return True
    if not isinstance(platform, GroupAdminPlatform):
        return False
    try:
        return bool(await platform.is_group_admin(scope.chat, user))
    except Exception:
        return False
