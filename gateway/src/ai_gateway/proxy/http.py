"""ASGI wrappers around the SDK's Streamable HTTP endpoint."""

import json
import logging
from uuid import UUID

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.types import INVALID_REQUEST
from mcp.types.version import HANDSHAKE_PROTOCOL_VERSIONS
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_gateway.proxy.sessions import UpstreamSessionPool

logger = logging.getLogger(__name__)

PINNED_PROTOCOL_VERSION = "2025-11-25"
"""The MCP specification revision this gateway is built and tested against."""

ACCEPTED_PROTOCOL_VERSIONS = frozenset(HANDSHAKE_PROTOCOL_VERSIONS)
"""Revisions negotiated through the initialize handshake. The SDK settles on the newest
one the client offers, which is the pinned revision for any current client."""

_PROTOCOL_VERSION_HEADER = "mcp-protocol-version"
_SESSION_ID_HEADER = "mcp-session-id"


class ProtocolVersionGuard:
    """Reject requests that declare a protocol revision the gateway does not speak.

    The SDK would route a 2026-07-28 ("modern", stateless) request to a different code
    path with no sessions, so the per-session upstream handling would not apply. Until a
    later phase adopts that revision, such requests get a clear 400.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        declared = (
            Headers(scope=scope).get(_PROTOCOL_VERSION_HEADER) if scope["type"] == "http" else None
        )
        if declared is None or declared in ACCEPTED_PROTOCOL_VERSIONS:
            await self._app(scope, receive, send)
            return

        body = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": INVALID_REQUEST,
                "message": (
                    f"Unsupported MCP protocol version {declared[:40]!r}."
                    f" This gateway speaks {PINNED_PROTOCOL_VERSION}."
                ),
            },
        }
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(body).encode()})


class SessionCleanup:
    """Close a session's upstream connections when its client ends the session.

    Only a DELETE the SDK accepted counts: the SDK answers 404 when the session id is
    unknown or belongs to another credential, and those must not close anything.
    """

    def __init__(self, app: ASGIApp, pool: UpstreamSessionPool) -> None:
        self._app = app
        self._pool = pool

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "DELETE":
            await self._app(scope, receive, send)
            return

        status: int | None = None

        async def capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self._app(scope, receive, capture_status)

        session_id = Headers(scope=scope).get(_SESSION_ID_HEADER)
        user = scope.get("user")
        if (
            session_id
            and isinstance(user, AuthenticatedUser)
            and status is not None
            and status < 400
        ):
            self._pool.close_session(session_id, UUID(user.access_token.client_id))
