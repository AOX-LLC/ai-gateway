"""Strict tool registration for low-level MCP servers.

The SDK's high-level MCPServer (2.2.0) silently drops arguments a tool did not declare and
publishes schemas without `additionalProperties: false`. For tools that write to a
company's systems that is the wrong default, so these servers use the low-level `Server`
and this toolset: every input is a Pydantic model that forbids extra fields, arguments
are validated before any handler runs, and a bad call is a protocol error (-32602) with a
short message that never echoes the arguments.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from mcp_common.attribution import client_name_from_meta

logger = logging.getLogger(__name__)

_MAX_ECHOED_NAME_LENGTH = 64

StrictInput = ConfigDict(extra="forbid")
"""The model_config every tool input model uses, so its schema says additionalProperties: false."""


NO_CONTROL_CHARACTERS = r"^[^\x00-\x1f\x7f-\x9f]*$"
"""A `pattern` for free-text inputs: any character except the control characters (NUL, tab,
newline and the rest; an ordinary space is fine). NUL in particular would reach the database
driver and fail there as an internal error instead of a bad request."""


class NoArguments(BaseModel):
    """The input model of a tool that takes no arguments."""

    model_config = StrictInput


class ToolError(Exception):
    """A failure the model should see and can react to: returned as an error result,
    not a protocol error. The message must be safe to show the client."""


@dataclass(frozen=True)
class CallInfo:
    requested_by: str
    """The client name from the request's `_meta`, or "direct". Attribution only."""


@dataclass(frozen=True)
class _RegisteredTool:
    tool: Tool
    prepare: Callable[[dict[str, Any]], Callable[[CallInfo], Awaitable[BaseModel]]]
    """Validates the arguments now, and returns the call to run with them."""


class StrictToolset:
    def __init__(self) -> None:
        self._tools: dict[str, _RegisteredTool] = {}

    def register[I: BaseModel, O: BaseModel](
        self,
        name: str,
        description: str,
        input_model: type[I],
        output_model: type[O],
        handler: Callable[[I, CallInfo], Awaitable[O]],
        *,
        read_only: bool,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        if input_model.model_config.get("extra") != "forbid":
            raise ValueError(f"the input model of {name!r} must set extra='forbid'")

        def prepare(arguments: dict[str, Any]) -> Callable[[CallInfo], Awaitable[BaseModel]]:
            validated = input_model.model_validate(arguments, strict=True)
            return lambda info: handler(validated, info)

        tool = Tool(
            name=name,
            description=description,
            input_schema=input_model.model_json_schema(),
            output_schema=output_model.model_json_schema(),
            annotations=ToolAnnotations(read_only_hint=read_only),
        )
        self._tools[name] = _RegisteredTool(tool, prepare)

    def tools(self) -> list[Tool]:
        return [registered.tool for registered in self._tools.values()]

    async def list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=self.tools())

    async def call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: CallToolRequestParams
    ) -> CallToolResult:
        registered = self._tools.get(params.name)
        if registered is None:
            raise MCPError(INVALID_PARAMS, f"Unknown tool '{_displayable(params.name)}'.")
        try:
            run = registered.prepare(params.arguments or {})
        except ValidationError:
            # Not echoed: the error text would repeat the client's own values back.
            raise MCPError(INVALID_PARAMS, f"Invalid arguments for tool '{params.name}'.") from None
        try:
            output = await run(CallInfo(requested_by=client_name_from_meta(ctx.meta)))
        except ToolError as error:
            return CallToolResult(
                content=[TextContent(type="text", text=str(error))], is_error=True
            )
        except MCPError:
            raise
        except Exception:
            # The SDK would put str(exc) in the error sent to the client.
            logger.exception("tool %s failed", params.name)
            raise MCPError(INTERNAL_ERROR, "Internal server error.") from None
        return _result(output)

    def build_server(self, name: str, version: str, instructions: str) -> Server[Any]:
        return Server(
            name,
            version=version,
            instructions=instructions,
            on_list_tools=self.list_tools,
            on_call_tool=self.call_tool,
        )


def _result(output: BaseModel) -> CallToolResult:
    structured = output.model_dump(mode="json")
    text = json.dumps(structured, indent=2)
    return CallToolResult(
        content=[TextContent(type="text", text=text)], structured_content=structured
    )


def _displayable(name: str) -> str:
    if len(name) <= _MAX_ECHOED_NAME_LENGTH:
        return name
    return name[: _MAX_ECHOED_NAME_LENGTH - 3] + "..."
