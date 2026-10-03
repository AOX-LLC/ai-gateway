"""The canonical layer order. Configuration switches layers on and off; it never reorders
them, because the order is itself a security property.

The layers so far, in order: scope → allowlist → rate_limit → approval. Later phases slot theirs in
between: schema → pinned_descriptions → egress → canary → classifier come before approval.
Approval stays last before forwarding, so a human approves exactly the call that runs.
"""

from ai_gateway.pipeline.layers.allowlist import AllowlistLayer
from ai_gateway.pipeline.layers.approval import ApprovalLayer
from ai_gateway.pipeline.layers.rate_limit import RateLimitLayer
from ai_gateway.pipeline.layers.scope import ScopeLayer
from ai_gateway.pipeline.types import BaseLayer

LAYER_ORDER: tuple[type[BaseLayer], ...] = (
    ScopeLayer,
    AllowlistLayer,
    RateLimitLayer,
    ApprovalLayer,
)
