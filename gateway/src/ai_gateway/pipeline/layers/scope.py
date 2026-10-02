"""The scope layer: a client sees and calls only the tools it was granted."""

from collections.abc import Sequence

from ai_gateway.pipeline.types import (
    ALLOW,
    BaseLayer,
    CallContext,
    CatalogTool,
    Deny,
    DenyCode,
    ToolCall,
    Verdict,
    tool_unavailable_message,
)


class ScopeLayer(BaseLayer):
    name = "scope"
    floor = True

    async def filter_tools(
        self, ctx: CallContext, tools: Sequence[CatalogTool]
    ) -> Sequence[CatalogTool]:
        return [tool for tool in tools if tool.exposed_name in ctx.client.scopes]

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        if call.exposed_name in ctx.client.scopes:
            return ALLOW
        return Deny(DenyCode.TOOL_UNAVAILABLE, tool_unavailable_message(call.exposed_name))
