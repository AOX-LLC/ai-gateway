"""Assemble the lab upstream: a lenient low-level MCP server behind the service credential, and
two routes the oracle reads (`GET /effects`, `POST /effects/reset`) behind the same credential."""

from typing import Any

from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from lab_upstream.settings import LabSettings
from lab_upstream.tools import PHASES, EffectLog, definitions, run
from mcp_common.credentials import ServiceCredentialMiddleware
from mcp_common.http_app import build_mcp_app

_MAX_ECHOED_NAME = 64


class LabNotEnabledError(RuntimeError):
    """`LAB_MUTABLE_UPSTREAM` is not `yes`: the lab upstream must not start."""


def build_app(settings: LabSettings) -> Starlette:
    """The lab upstream's app. Without the switch, with an unknown phase, or with a missing or weak
    credential, it raises here, before any request is accepted."""
    if settings.mutable_upstream != "yes":
        raise LabNotEnabledError(
            "LAB_MUTABLE_UPSTREAM must be exactly 'yes': this server is test tooling for the lab"
            " profile and refuses to run otherwise"
        )
    if settings.phase not in PHASES:
        raise ValueError(f"the lab upstream's phase must be one of {list(PHASES)}")
    phase = settings.phase
    offered = {tool.name: tool for tool in definitions(phase)}
    log = EffectLog(phase)

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=list(offered.values()))

    async def call_tool(
        ctx: ServerRequestContext[Any, Any], params: CallToolRequestParams
    ) -> CallToolResult:
        if params.name not in offered:
            shown = params.name[:_MAX_ECHOED_NAME]
            raise MCPError(INVALID_PARAMS, f"Unknown tool '{shown}'.")
        arguments = params.arguments or {}
        log.record(params.name, arguments)
        return run(params.name, arguments)

    server: Server[Any] = Server(
        "lab",
        version="0.1.0.dev0",
        instructions="Lab upstream for the red-team scorecard. Fictional; never a product server.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )

    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "server": "lab-upstream", "phase": phase})

    token = settings.service_token.get_secret_value()
    app = build_mcp_app(
        server, allowed_hosts=settings.allowed_hosts, credential=token, health=health
    )

    async def effects(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        if request.url.path.endswith("/reset"):
            if request.method != "POST":
                response = JSONResponse({"error": "method not allowed"}, status_code=405)
            else:
                log.reset()
                response = JSONResponse({"status": "reset"})
        else:
            response = JSONResponse(log.snapshot())
        await response(scope, receive, send)

    guarded = ServiceCredentialMiddleware(effects, token)
    app.router.routes.append(Route("/effects", endpoint=guarded, methods=["GET"]))
    app.router.routes.append(Route("/effects/reset", endpoint=guarded, methods=["POST"]))
    return app
