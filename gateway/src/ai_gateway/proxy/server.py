"""The MCP server clients talk to: tools/list and tools/call, both through the pipeline."""

import logging
from typing import Any
from uuid import UUID, uuid4

from mcp.server import Server, ServerRequestContext
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
)
from starlette.requests import Request

from ai_gateway.pipeline.runner import (
    Blocked,
    Pipeline,
    UpstreamOutcome,
    UpstreamStatus,
)
from ai_gateway.pipeline.types import CallContext, ClientIdentity, Deny, DenyCode, ToolCall
from ai_gateway.proxy.catalog import Catalog
from ai_gateway.proxy.sessions import UpstreamCallError, UpstreamSessionPool

logger = logging.getLogger(__name__)

POLICY_BLOCKED = -32010
"""JSON-RPC code for calls a pipeline layer blocked, other than unavailable tools."""

_UPSTREAM_FAILURE_MESSAGES = {
    UpstreamStatus.TIMEOUT: "The '{namespace}' service did not answer in time.",
    UpstreamStatus.UNAVAILABLE: "The '{namespace}' service is unavailable.",
    UpstreamStatus.REJECTED: "The '{namespace}' service rejected the call.",
    UpstreamStatus.INVALID_RESPONSE: "The '{namespace}' service returned an invalid response.",
}


class GatewayServer:
    def __init__(self, catalog: Catalog, sessions: UpstreamSessionPool, pipeline: Pipeline) -> None:
        self._catalog = catalog
        self._sessions = sessions
        self._pipeline = pipeline

    def build(self) -> Server[Any]:
        return Server(
            "ai-gateway",
            version="0.1.0",
            instructions=(
                "Tools from the fictional Harborline Supply Co., served through a gateway"
                " that checks every call."
            ),
            on_list_tools=self.list_tools,
            on_call_tool=self.call_tool,
        )

    async def list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        call_ctx = _call_context(ctx)
        try:
            visible = await self._pipeline.list_tools(call_ctx, self._catalog.tools())
        except Exception:
            # The SDK would put str(exc) in the error sent to the client.
            logger.exception("tools/list failed (request %s)", call_ctx.request_id)
            raise _internal_error(call_ctx.request_id) from None
        # The whole catalog fits in one page, so cursors are not used.
        return ListToolsResult(tools=[tool.tool for tool in visible])

    async def call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: CallToolRequestParams
    ) -> CallToolResult:
        call_ctx = _call_context(ctx)
        try:
            return await self._call_tool(call_ctx, params)
        except MCPError:
            raise
        except Exception:
            logger.exception("tools/call failed (request %s)", call_ctx.request_id)
            raise _internal_error(call_ctx.request_id) from None

    async def _call_tool(
        self, call_ctx: CallContext, params: CallToolRequestParams
    ) -> CallToolResult:
        resolved = self._catalog.resolve(params.name)
        if resolved is None:
            deny = await self._pipeline.reject_unknown_tool(call_ctx, params.name)
            raise _deny_error(deny, call_ctx.request_id)

        call = ToolCall.create(
            exposed_name=params.name,
            namespace=resolved.upstream.namespace,
            upstream_tool=resolved.tool.upstream_name,
            arguments=params.arguments or {},
            effect=resolved.tool.effect,
            effect_source=resolved.tool.effect_source,
        )

        async def forward(ctx: CallContext, approved_call: ToolCall) -> UpstreamOutcome:
            try:
                result = await self._sessions.call_tool(
                    ctx.session_id,
                    ctx.client.id,
                    resolved.upstream,
                    approved_call.upstream_tool,
                    approved_call.arguments,
                    ctx.client.name,
                )
            except UpstreamCallError as error:
                logger.warning("request %s: %s", ctx.request_id, error, exc_info=error.__cause__)
                return UpstreamOutcome(_upstream_failure(error, ctx.request_id), error.status)
            status = UpstreamStatus.TOOL_ERROR if result.is_error else UpstreamStatus.OK
            return UpstreamOutcome(result, status)

        outcome = await self._pipeline.call_tool(call_ctx, call, forward)
        if isinstance(outcome, Blocked):
            raise _deny_error(outcome.deny, call_ctx.request_id)
        return outcome.result


def _call_context(ctx: ServerRequestContext[Any, Any]) -> CallContext:
    access_token = get_access_token()
    if access_token is None:
        # BearerAuthMiddleware runs before every request; reaching here is a wiring bug.
        raise MCPError(INVALID_REQUEST, "Authentication required.")

    claims = access_token.claims or {}
    session_id = (
        ctx.request.headers.get("mcp-session-id") if isinstance(ctx.request, Request) else None
    )
    return CallContext(
        request_id=uuid4(),
        client=ClientIdentity(
            id=UUID(access_token.client_id),
            name=str(claims.get("client_name", "")),
            scopes=frozenset(access_token.scopes),
        ),
        session_id=session_id,
        protocol_version=ctx.protocol_version,
    )


def _deny_error(deny: Deny, request_id: UUID) -> MCPError:
    if deny.code is DenyCode.TOOL_UNAVAILABLE:
        return MCPError(INVALID_PARAMS, deny.public_message)
    return MCPError(POLICY_BLOCKED, deny.public_message, {"request_id": str(request_id)})


def _internal_error(request_id: UUID) -> MCPError:
    return MCPError(INTERNAL_ERROR, "Internal gateway error.", {"request_id": str(request_id)})


def _upstream_failure(error: UpstreamCallError, request_id: UUID) -> CallToolResult:
    message = _UPSTREAM_FAILURE_MESSAGES[error.status].format(namespace=error.namespace)
    return CallToolResult(
        content=[TextContent(type="text", text=f"{message} (request {request_id})")],
        is_error=True,
    )
