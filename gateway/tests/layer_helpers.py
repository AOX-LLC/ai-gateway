"""Hand-made calls, contexts and tools for the injection-layer tests."""

import json
from typing import Any, cast
from uuid import uuid4

from mcp.types import CallToolResult, TextContent, Tool

from ai_gateway.pipeline.config import parse_pipeline_config
from ai_gateway.pipeline.pins import ToolPins, parse_tool_pins, render_tool_pins
from ai_gateway.pipeline.runner import Pipeline, UpstreamOutcome, UpstreamStatus
from ai_gateway.pipeline.types import (
    BaseLayer,
    CallContext,
    CatalogTool,
    ClientIdentity,
    ToolCall,
    ToolDefinition,
)
from ai_gateway.seams.events import MemoryEventSink

TICKET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "maxLength": 100},
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        "count": {"type": "integer", "minimum": 0, "maximum": 10},
    },
    "required": ["subject"],
    "additionalProperties": False,
}
DESCRIPTION = "Opens a ticket."
TOOL = "tickets__create_ticket"


def pins_for(
    name: str = TOOL, description: str = DESCRIPTION, schema: dict[str, Any] | None = None
) -> ToolPins:
    text = render_tool_pins([(name, description, schema or TICKET_SCHEMA, None)])
    import tomllib

    return parse_tool_pins(tomllib.loads(text))


def ctx(client: str = "harborline-support-bot", session: str | None = "s1") -> CallContext:
    return CallContext(
        request_id=uuid4(),
        client=ClientIdentity(
            id=_CLIENT_IDS.setdefault(client, uuid4()), name=client, scopes=frozenset()
        ),
        session_id=session,
        protocol_version="2025-11-25",
    )


_CLIENT_IDS: dict[str, Any] = {}


def call(
    arguments: dict[str, Any],
    *,
    name: str = TOOL,
    effect: str = "write",
    description: str = DESCRIPTION,
    schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> ToolCall:
    return ToolCall.create(
        name,
        name.split("__")[0],
        name.split("__")[1],
        arguments,
        effect,  # type: ignore[arg-type]
        "policy",
        definition=ToolDefinition(
            description=description,
            input_schema_json=json.dumps(schema or TICKET_SCHEMA, sort_keys=True),
            output_schema_json=json.dumps(output_schema, sort_keys=True),
        ),
    )


def catalog_tool(
    name: str = TOOL,
    description: str = DESCRIPTION,
    schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> CatalogTool:
    return CatalogTool(
        namespace=name.split("__")[0],
        upstream_name=name.split("__")[1],
        tool=Tool(
            name=name,
            description=description,
            input_schema=schema or TICKET_SCHEMA,
            output_schema=output_schema,
        ),
    )


def result(text: str = "", structured: dict[str, Any] | None = None) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)] if text else [],
        structured_content=structured,
        is_error=False,
    )


def pipeline_with(
    layer: type[BaseLayer], mode: str, events: MemoryEventSink, **build: Any
) -> Pipeline:
    config = parse_pipeline_config({"layers": {layer.name: mode}}, [layer])
    return Pipeline.build(config, events, layer_order=[layer], **build)


async def run(pipeline: Pipeline, tool_call: ToolCall, answer: CallToolResult | None = None) -> Any:
    async def forward(_: CallContext, __: ToolCall) -> UpstreamOutcome:
        return UpstreamOutcome(answer or result("ok"), UpstreamStatus.OK)

    return await pipeline.call_tool(ctx(), tool_call, forward)


def last_layer(events: MemoryEventSink) -> dict[str, Any]:
    """The first layer verdict of the newest decision record, as a plain dict."""
    layers = cast(list[dict[str, Any]], events.events[-1].payload["layers"])
    return layers[0]
