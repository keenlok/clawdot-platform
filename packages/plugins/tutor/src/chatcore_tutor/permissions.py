"""Live group manager checks for tutor-topic changes."""

import logging

from chatcore.access import AccessPolicy
from chatcore.events import ChatRef, UserRef
from chatcore.group_access import GroupAdminPlatform
from chatcore.platform import ChatPlatform

log = logging.getLogger(__name__)


async def is_group_manager(
    access: AccessPolicy,
    platform: ChatPlatform,
    chat: ChatRef,
    user: UserRef,
) -> bool:
    if user.platform != chat.platform or platform.name != chat.platform:
        return False
    refreshed = True
    try:
        await access.refresh()
    except Exception as exc:
        refreshed = False
        log.warning(
            "tutor manager access refresh failed chat=%s user=%s error=%s",
            chat.key,
            user.key,
            type(exc).__name__,
        )
    if refreshed and access.can_setup(user, chat):
        return True
    if not isinstance(platform, GroupAdminPlatform):
        return False
    try:
        return bool(await platform.is_group_admin(chat, user))
    except Exception as exc:
        log.warning(
            "tutor manager check failed chat=%s user=%s error=%s",
            chat.key,
            user.key,
            type(exc).__name__,
        )
        return False
