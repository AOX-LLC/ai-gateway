"""The canonical layer order. Configuration switches layers on and off; it never reorders
them, because the order is itself a security property.

Later phases slot their layers in here:
    scope → allowlist → rate_limit → schema → pinned_descriptions → egress → canary
    → classifier → approval
Approval stays last before forwarding, so a human approves exactly the call that runs.
"""

from ai_gateway.pipeline.layers.approval import ApprovalLayer
from ai_gateway.pipeline.layers.scope import ScopeLayer
from ai_gateway.pipeline.types import BaseLayer

LAYER_ORDER: tuple[type[BaseLayer], ...] = (ScopeLayer, ApprovalLayer)
