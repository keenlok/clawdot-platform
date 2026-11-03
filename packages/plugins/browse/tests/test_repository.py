from types import SimpleNamespace

import pytest
from chatcore_browse.repository import BrowseRepo
from chatcore_browse.settings import BrowseSettings
from chatcore_jobs.models import Scope


class Db:
    def __init__(self, row=None):
        self.row = row

    async def fetchrow(self, _query, _chat_key, _is_dm):
        return self.row


@pytest.mark.parametrize(
    ("is_dm", "dm_enabled", "expected"),
    [(False, False, True), (False, True, True), (True, False, False), (True, True, True)],
)
async def test_missing_setting_defaults_on_for_groups_and_respects_dm_setting(
    is_dm, dm_enabled, expected
):
    repo = BrowseRepo(Db(), BrowseSettings(dm_enabled=dm_enabled))
    result = await repo.settings(Scope("tg:test", is_dm))
    assert result == {"rendered_enabled": expected, "revision": 0}


@pytest.mark.parametrize("enabled", [True, False])
async def test_saved_group_choice_overrides_the_default(enabled):
    row = {"rendered_enabled": enabled, "revision": 3, "updated_by": "tg:manager"}
    repo = BrowseRepo(Db(row), BrowseSettings())
    assert await repo.settings(Scope("tg:test", False)) == row


async def test_store_failure_does_not_silently_enable_rendering():
    async def unavailable(*_args):
        raise RuntimeError("store unavailable")

    repo = BrowseRepo(SimpleNamespace(fetchrow=unavailable), BrowseSettings())
    with pytest.raises(RuntimeError, match="store unavailable"):
        await repo.settings(Scope("tg:test", False))
