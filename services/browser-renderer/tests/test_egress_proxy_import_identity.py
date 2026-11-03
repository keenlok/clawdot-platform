from chatcore.security.egress_proxy import (
    Connector as CoreConnector,
)
from chatcore.security.egress_proxy import (
    EgressPolicy as CoreEgressPolicy,
)
from chatcore.security.egress_proxy import (
    EgressProxy as CoreEgressProxy,
)
from chatcore.security.egress_proxy import (
    PolicyOverrides as CorePolicyOverrides,
)
from chatcore.security.egress_proxy import (
    RenderBudget as CoreRenderBudget,
)
from chatcore.security.egress_proxy import (
    Resolver as CoreResolver,
)
from chatcore.security.egress_proxy.policy import PinnedTarget as CorePinnedTarget
from chatcore_browser_renderer.budget import RenderBudget
from chatcore_browser_renderer.policy import (
    Connector,
    EgressPolicy,
    PinnedTarget,
    PolicyOverrides,
    Resolver,
)
from chatcore_browser_renderer.proxy import EgressProxy


def test_renderer_compatibility_imports_keep_core_object_identity() -> None:
    assert EgressProxy is CoreEgressProxy
    assert EgressPolicy is CoreEgressPolicy
    assert PolicyOverrides is CorePolicyOverrides
    assert RenderBudget is CoreRenderBudget
    assert PinnedTarget is CorePinnedTarget
    assert Connector is CoreConnector
    assert Resolver is CoreResolver
