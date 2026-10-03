"""Bearer-token authentication in front of the MCP endpoint.

The gateway uses its own middleware rather than the SDK's RequireAuthMiddleware so it
can follow RFC 6750 exactly (no error code when the token is missing) and emit a
security event with the failure reason. On success it sets the SDK's AuthenticatedUser,
which the SDK's session manager uses to bind a session to the client that opened it.

Authentication sits outside the request pipeline: no configuration can switch it off.
"""

import json
import logging
from math import ceil

import anyio
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.authentication import AuthCredentials
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from ai_gateway.auth.throttle import LoginThrottle
from ai_gateway.auth.tokens import parse_lookup_id
from ai_gateway.auth.verifier import AuthFailure, AuthFailureReason, TokenVerifier
from ai_gateway.seams.events import DEFAULT_EMIT_TIMEOUT_S, EventSink, GatewayEvent

_REALM = 'Bearer realm="ai-gateway"'
_UNAUTHORIZED_BODY = json.dumps(
    {"error": "unauthorized", "message": "A valid bearer token is required."}
).encode()

_THROTTLED_BODY = json.dumps(
    {"error": "too_many_attempts", "message": "Too many failed attempts. Retry later."}
).encode()

ANONYMOUS_ACTOR = "anonymous"

logger = logging.getLogger(__name__)


class BearerAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        verifier: TokenVerifier,
        events: EventSink,
        emit_timeout_s: float = DEFAULT_EMIT_TIMEOUT_S,
        throttle: LoginThrottle | None = None,
    ) -> None:
        self._app = app
        self._verifier = verifier
        self._events = events
        self._emit_timeout_s = emit_timeout_s
        self._throttle = throttle

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        presented = _bearer_token(Headers(scope=scope))
        lookup_id = parse_lookup_id(presented) if presented is not None else None
        if self._throttle is not None:
            retry_in_s = self._throttle.check(lookup_id)
            if retry_in_s is not None:
                # Refused without looking at the secret, and not counted as another failure.
                await self._emit_failure(AuthFailure(AuthFailureReason.THROTTLED, lookup_id), scope)
                await _send_throttled(send, retry_in_s)
                return
        result = (
            await self._verifier.verify(presented)
            if presented is not None
            else AuthFailure(AuthFailureReason.MISSING)
        )
        if isinstance(result, AuthFailure):
            if self._throttle is not None:
                self._throttle.record_failure(lookup_id)
            await self._emit_failure(result, scope)
            await _send_unauthorized(send, token_was_presented=presented is not None)
            return

        if self._throttle is not None:
            self._throttle.record_success(result.lookup_id)
        access_token = result.to_access_token()
        scope["user"] = AuthenticatedUser(access_token)
        scope["auth"] = AuthCredentials(access_token.scopes)
        await self._app(scope, receive, send)

    async def _emit_failure(self, failure: AuthFailure, scope: Scope) -> None:
        # The direct peer address. Behind a tunnel or proxy this is the proxy; trusting a
        # forwarded-for header is a decision for whichever phase puts one in front.
        client_address = scope.get("client")
        # Recording is best effort: a sink that raises or does not answer in time must not
        # delay or fail the 401, which is sent either way.
        try:
            event = GatewayEvent(
                action="gateway.auth_failure",
                actor_id=ANONYMOUS_ACTOR,
                payload={
                    "reason": failure.reason.value,
                    "lookup_id": failure.lookup_id,
                    "remote_addr": client_address[0] if client_address else None,
                },
            )
            with anyio.fail_after(self._emit_timeout_s):
                await self._events.emit(event)
        except Exception:
            # Catching broadly is deliberate. The traceback is logged; the event is not.
            logger.exception("could not record a gateway.auth_failure event")


def _bearer_token(headers: Headers) -> str | None:
    authorization = headers.get("authorization")
    if authorization is None:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return None
    return credentials.strip()


async def _send_throttled(send: Send, retry_in_s: float) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(max(1, ceil(retry_in_s))).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _THROTTLED_BODY})


async def _send_unauthorized(send: Send, *, token_was_presented: bool) -> None:
    challenge = f'{_REALM}, error="invalid_token"' if token_was_presented else _REALM
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", challenge.encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
