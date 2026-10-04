"""The canonical layer order. Configuration switches layers on and off; it never reorders
them, because the order is itself a security property.

The layers, in order: scope → allowlist → rate_limit → schema → pinned_descriptions → egress →
canary → (classifier, Phase 4c) → approval. Approval stays last before forwarding, so a human
approves exactly the call that runs, and a call the injection layers refuse never asks anyone.
"""

from ai_gateway.pipeline.layers.allowlist import AllowlistLayer
from ai_gateway.pipeline.layers.approval import ApprovalLayer
from ai_gateway.pipeline.layers.canary import CanaryLayer
from ai_gateway.pipeline.layers.egress import EgressLayer
from ai_gateway.pipeline.layers.pinned import PinnedDescriptionsLayer
from ai_gateway.pipeline.layers.rate_limit import RateLimitLayer
from ai_gateway.pipeline.layers.schema import SchemaLayer
from ai_gateway.pipeline.layers.scope import ScopeLayer
from ai_gateway.pipeline.types import BaseLayer

LAYER_ORDER: tuple[type[BaseLayer], ...] = (
    ScopeLayer,
    AllowlistLayer,
    RateLimitLayer,
    SchemaLayer,
    PinnedDescriptionsLayer,
    EgressLayer,
    CanaryLayer,
    ApprovalLayer,
)
