"""The service credential: how an MCP server knows a request comes from the gateway.

Each server holds one secret in its environment and the gateway is configured with the
same value (by environment variable name, in the upstream registry). A server refuses to
start without it, so it can never run open by accident.
"""

import hmac
import os

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_BEARER_PREFIX = "bearer "


class MissingCredentialError(RuntimeError):
    """The server's service credential is not configured; it must not start."""


def credential_from_env(name: str) -> str:
    """The credential in the named environment variable; raise when it is missing or empty."""
    value = os.environ.get(name, "")
    if not value.strip():
        raise MissingCredentialError(f"{name} is not set; refusing to start without it")
    return value


class ServiceCredentialMiddleware:
    """Require `Authorization: Bearer <credential>` on every HTTP request.

    The comparison is constant-time. Every failure gets the same bare 401, so a caller
    learns nothing about why it was refused.
    """

    def __init__(self, app: ASGIApp, credential: str) -> None:
        if not credential.strip():
            raise MissingCredentialError("the service credential is empty")
        self._app = app
        self._expected = credential.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._is_authorised(scope):
            await self._app(scope, receive, send)
            return
        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="mcp-server"'},
        )
        await response(scope, receive, send)

    def _is_authorised(self, scope: Scope) -> bool:
        for name, value in scope["headers"]:
            if name != b"authorization":
                continue
            header = value.decode("latin-1")
            if not header.lower().startswith(_BEARER_PREFIX):
                return False
            presented = header[len(_BEARER_PREFIX) :].strip().encode()
            return hmac.compare_digest(presented, self._expected)
        return False
