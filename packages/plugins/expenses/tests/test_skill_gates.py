import pytest
from chatcore.events import ChatRef
from chatcore.skills import GateState, SkillSet
from chatcore.testing import GROUP, FakePlatform, memory_stores, user
from chatcore.tools.registry import ToolContext, ToolRegistry, TurnState
from chatcore_expenses.plugin import ExpensesPlugin


@pytest.mark.asyncio
async def test_expense_gates_use_fresh_exactly_one_group_resolver():
    actor = user()
    linked = GROUP

    async def resolver(_ctx):
        return linked

    plugin = ExpensesPlugin(group_for_dm=resolver)
    skills = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    stores, platform = memory_stores(), FakePlatform()
    group = ToolContext(GROUP, actor, False, platform, stores, turn=TurnState())
    dm = ToolContext(ChatRef(actor.ref.platform, actor.ref.id), actor, True, platform, stores)

    assert (await skills.evaluate_skill("expenses", group)).state is GateState.AVAILABLE
    assert (await skills.evaluate_skill("expenses", dm)).state is GateState.AVAILABLE
    linked = None
    denied = await skills.evaluate_skill("expenses", dm)
    assert denied.state is GateState.NOT_ALLOWED
    assert denied.hints == (
        "Expenses work in a group, or in a DM once you're linked to exactly one group.",
    )
    linked = ChatRef(actor.ref.platform, "group-2")
    assert (await skills.evaluate_skill("trips", dm)).state is GateState.AVAILABLE


@pytest.mark.asyncio
async def test_expense_dm_without_a_resolver_is_denied():
    actor = user()
    plugin = ExpensesPlugin()
    skills = SkillSet.build([plugin], ToolRegistry(plugin.tools()))
    dm = ToolContext(
        ChatRef(actor.ref.platform, actor.ref.id),
        actor,
        True,
        FakePlatform(),
        memory_stores(),
    )
    result = await skills.evaluate_skill("expenses", dm)
    assert result.state is GateState.NOT_ALLOWED
    assert result.hints == (
        "Expenses work in a group, or in a DM once you're linked to exactly one group.",
    )
