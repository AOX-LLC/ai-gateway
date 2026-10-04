"""The pinned_descriptions layer: a tool is offered and called only while its description and its
input and output schemas are the ones a person reviewed.

Detection: the hash of the tool's name, description and both schemas (what the catalog holds now)
against the pin in `config/tool_pins.toml`. A tool whose hash differs is drifted; a tool with no pin
is unpinned (a new tool is unreviewed, as one with no effect policy is a write). Either is hidden
from tools/list and refused on a call, and an alert is raised once per tool and definition.

False positives: a deployment that legitimately changes a description hides the tool until someone
pins it again. Monitor mode records `would_block` and leaves the tool in place.
"""

from collections.abc import Sequence

from ai_gateway.pipeline.alerts import Alerts
from ai_gateway.pipeline.pins import ToolPins, definition_sha256
from ai_gateway.pipeline.types import (
    ALLOW,
    POLICY_BLOCK_MESSAGE,
    BaseLayer,
    CallContext,
    CatalogTool,
    Deny,
    DenyCode,
    ToolCall,
    Verdict,
)


class PinnedDescriptionsLayer(BaseLayer):
    name = "pinned_descriptions"

    def __init__(self, pins: ToolPins | None = None, alerts: Alerts | None = None) -> None:
        self._pins = pins if pins is not None else ToolPins({})
        self._alerts = alerts or Alerts()

    def _verdict_for(self, name: str, current_sha256: str) -> DenyCode | None:
        pin = self._pins.get(name)
        if pin is None:
            return DenyCode.PIN_UNPINNED
        if pin.sha256 != current_sha256:
            return DenyCode.PIN_DRIFT
        return None

    def _alert(self, ctx: CallContext, name: str, code: DenyCode, sha256: str) -> None:
        self._alerts.raise_alert(
            code.value,
            tool=name,
            client=ctx.client.name,
            detail={"definition_sha256": sha256},
        )

    async def filter_tools(
        self, ctx: CallContext, tools: Sequence[CatalogTool]
    ) -> Sequence[CatalogTool]:
        kept = []
        for tool in tools:
            sha256 = definition_sha256(
                tool.exposed_name,
                tool.tool.description,
                tool.tool.input_schema,
                tool.tool.output_schema,
            )
            code = self._verdict_for(tool.exposed_name, sha256)
            if code is None:
                kept.append(tool)
            else:
                self._alert(ctx, tool.exposed_name, code, sha256)
        return kept

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        if call.definition is None:
            return Deny(DenyCode.PIN_UNPINNED, POLICY_BLOCK_MESSAGE, score=1)
        sha256 = definition_sha256(
            call.exposed_name,
            call.definition.description,
            call.definition.input_schema,
            call.definition.output_schema,
        )
        code = self._verdict_for(call.exposed_name, sha256)
        if code is None:
            return ALLOW
        self._alert(ctx, call.exposed_name, code, sha256)
        return Deny(code, POLICY_BLOCK_MESSAGE, score=1)
