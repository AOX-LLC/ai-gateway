"""The per-client session cap, against a stand-in for the SDK's session manager."""

from itertools import count
from uuid import uuid4

import httpx2
import pytest
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ai_gateway.proxy.http import SessionAdmission

_session_numbers = count()


async def fake_session_manager(scope: Scope, receive: Receive, send: Send) -> None:
    """Issues a new session id on a request without one, like the SDK's manager."""
    headers = {}
    if Headers(scope=scope).get("mcp-session-id") is None:
        headers["mcp-session-id"] = f"session-{next(_session_numbers)}"
    await Response("{}", headers=headers)(scope, receive, send)


def authenticated_as(app: ASGIApp) -> ASGIApp:
    async def with_user(scope: Scope, receive: Receive, send: Send) -> None:
        client_id = Headers(scope=scope)["x-test-client"]
        access_token = AccessToken(token="lookupid", client_id=client_id, scopes=[])  # noqa: S106
        scope["user"] = AuthenticatedUser(access_token)
        await app(scope, receive, send)

    return with_user


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _client(clock: Clock) -> httpx2.AsyncClient:
    admission = SessionAdmission(
        fake_session_manager, max_sessions_per_client=2, idle_timeout_s=60, clock=clock
    )
    transport = httpx2.ASGITransport(app=authenticated_as(admission))
    return httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1")


@pytest.mark.anyio
async def test_one_client_cannot_take_every_session(clock: Clock) -> None:
    greedy, polite = {"x-test-client": str(uuid4())}, {"x-test-client": str(uuid4())}

    async with _client(clock) as client:
        opened = [(await client.post("/mcp", headers=greedy)).status_code for _ in range(3)]
        other = await client.post("/mcp", headers=polite)

    assert opened == [200, 200, 429]
    assert other.status_code == 200


@pytest.mark.anyio
async def test_deleted_and_idle_sessions_free_their_slot(clock: Clock) -> None:
    headers = {"x-test-client": str(uuid4())}

    async with _client(clock) as client:
        first = await client.post("/mcp", headers=headers)
        await client.post("/mcp", headers=headers)
        assert (await client.post("/mcp", headers=headers)).status_code == 429

        session_id = first.headers["mcp-session-id"]
        await client.delete("/mcp", headers={**headers, "mcp-session-id": session_id})
        assert (await client.post("/mcp", headers=headers)).status_code == 200

        clock.now += 61
        assert (await client.post("/mcp", headers=headers)).status_code == 200
