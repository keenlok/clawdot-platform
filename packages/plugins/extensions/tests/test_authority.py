from __future__ import annotations

import pytest
from chatcore.events import ChatRef, UserRef
from chatcore_extensions.authority import ExtensionAuthority


@pytest.mark.asyncio
async def test_group_authority_uses_fresh_scoped_callback_and_fails_closed() -> None:
    group = ChatRef("tg", "-1001")
    user = UserRef("tg", "7")
    decisions = {True}
    authority = ExtensionAuthority(
        lambda chat: chat == group, lambda _chat, _user: _allowed(decisions)
    )

    assert await authority.may_manage(chat=group, user=user, is_dm=False)
    decisions.clear()
    assert not await authority.may_manage(chat=group, user=user, is_dm=False)
    assert not await authority.may_manage(chat=ChatRef("tg", "-1002"), user=user, is_dm=False)


async def _allowed(decisions: set[bool]) -> bool:
    return bool(decisions)


@pytest.mark.asyncio
async def test_dm_requires_exact_owner_and_no_group_role() -> None:
    user = UserRef("tg", "7")
    authority = ExtensionAuthority(lambda _chat: True, None)
    assert await authority.may_manage(chat=ChatRef("tg", "7"), user=user, is_dm=True)
    assert not await authority.may_manage(chat=ChatRef("tg", "8"), user=user, is_dm=True)
    assert not await authority.may_manage(chat=ChatRef("tg", "-1001"), user=user, is_dm=True)
