"""The service credential: how an MCP server knows a request comes from the gateway.

Each server holds one secret in its environment and the gateway is configured with the
same value (by environment variable name, in the upstream registry). A server refuses to
start without it, so it can never run open by accident, and refuses one that is short or
still the `change-me` placeholder of .env.example (`scripts/init_env.py` makes real ones).
"""

import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_BEARER_PREFIX = "bearer "
MIN_CREDENTIAL_LENGTH = 32
PLACEHOLDER_MARKER = "change-me"


class MissingCredentialError(RuntimeError):
    """The server's service credential is not configured; it must not start."""


class WeakCredentialError(RuntimeError):
    """The service credential is too short or is a placeholder; the server must not start."""


class ServiceCredentialMiddleware:
    """Require `Authorization: Bearer <credential>` on every HTTP request.

    The comparison is constant-time. Every failure gets the same bare 401, so a caller
    learns nothing about why it was refused.
    """

    def __init__(self, app: ASGIApp, credential: str) -> None:
        if not credential.strip():
            raise MissingCredentialError("the service credential is empty")
        if len(credential) < MIN_CREDENTIAL_LENGTH or PLACEHOLDER_MARKER in credential:
            raise WeakCredentialError(
                f"the service credential must be at least {MIN_CREDENTIAL_LENGTH} characters"
                f" and not a '{PLACEHOLDER_MARKER}' placeholder; run scripts/init_env.py"
            )
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
