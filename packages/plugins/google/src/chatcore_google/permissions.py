"""Shared runtime Google access checks; gates are an additional presentation layer."""

from chatcore.group_access import GroupAdminPlatform


async def authorize(settings, access, repo, ctx, *, write=False, configure=False):
    if not settings.configured:
        raise PermissionError("Google tools are not configured.")
    await access.refresh()
    if (
        ctx.chat.platform != ctx.user.ref.platform
        or ctx.platform.name != ctx.chat.platform
        or ctx.chat.id == "*"
        or not ctx.chat.id
    ):
        raise PermissionError("Google tools are unavailable in this scope.")
    if ctx.is_dm:
        allowed = await access.can_dm(ctx.user.ref, ctx.platform, ctx.stores.cache)
    else:
        allowed = access.is_chat_allowed(ctx.chat)
    if not allowed or access.restriction_for(ctx.user.ref, ctx.chat, is_dm=ctx.is_dm):
        raise PermissionError("Google tools are unavailable in this chat.")
    if configure:
        manager = (
            access.is_owner(ctx.user.ref) if ctx.is_dm else access.can_setup(ctx.user.ref, ctx.chat)
        )
        if not manager and not ctx.is_dm and isinstance(ctx.platform, GroupAdminPlatform):
            manager = await ctx.platform.is_group_admin(ctx.chat, ctx.user.ref)
        if not manager:
            raise PermissionError("A group manager or owner must change Google settings.")
    elif not (await repo.settings(ctx.chat.key))[0]:
        raise PermissionError("Google tools are off in this chat; ask a manager to turn them on.")
    if (
        write
        and not configure
        and not (access.is_admin(ctx.user.ref) or ctx.user.ref.key in settings.writers)
    ):
        raise PermissionError("Google writes require a bot admin or an explicitly allowed writer.")
