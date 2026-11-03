import pytest
from chatcore.display_time_plugin import DisplayTimePlugin
from chatcore.events import ChatRef
from chatcore.skills import GateState, SkillSet
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState


class AdminPlatform(FakePlatform):
    def __init__(self):
        super().__init__()
        self.admins = set()

    async def is_group_admin(self, chat, actor):
        return (chat.key, actor.key) in self.admins


@pytest.mark.asyncio
async def test_time_zone_group_admin_revocation_and_own_dm_rule_are_fresh():
    actor = user()
    platform = AdminPlatform()
    plugin = DisplayTimePlugin()
    skills = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    stores = memory_stores()
    ctx = ToolContext(GROUP, actor, False, platform, stores, turn=TurnState())

    platform.admins.add((GROUP.key, actor.ref.key))
    admin = await skills.evaluate_skill("time_zone", ctx)
    assert admin.state is GateState.AVAILABLE
    assert {"set_display_time_zone", "reset_display_time_zone"} <= set(admin.offered)
    platform.admins.clear()
    member = await skills.evaluate_skill("time_zone", ctx)
    assert member.state is GateState.AVAILABLE
    assert member.offered == ("get_display_time_zone",)
    assert member.hints == ("Only a current group administrator can change this group setting.",)

    own_dm = ToolContext(ChatRef(actor.ref.platform, actor.ref.id), actor, True, platform, stores)
    dm = await skills.evaluate_skill("time_zone", own_dm)
    assert {"set_display_time_zone", "reset_display_time_zone"} <= set(dm.offered)
    other_dm = ToolContext(ChatRef(actor.ref.platform, "other"), actor, True, platform, stores)
    denied = await skills.evaluate_skill("time_zone", other_dm)
    assert denied.offered == ("get_display_time_zone",)
    assert denied.state is GateState.AVAILABLE
