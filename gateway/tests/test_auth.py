"""Token verification and the bearer-auth middleware, against an in-memory registry."""

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from ai_gateway.auth.middleware import BearerAuthMiddleware
from ai_gateway.auth.tokens import IssuedToken, generate_token
from ai_gateway.auth.verifier import (
    AuthFailure,
    AuthFailureReason,
    TokenVerifier,
    VerifiedClient,
)
from ai_gateway.registry.models import ClientStatus, StoredToken
from ai_gateway.seams.events import MemoryEventSink

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class InMemoryRegistry:
    def __init__(self) -> None:
        self.tokens: dict[str, StoredToken] = {}
        self.uses: list[UUID] = []

    def add(self, token: IssuedToken, **overrides: object) -> StoredToken:
        stored = StoredToken(
            token_id=uuid4(),
            lookup_id=token.lookup_id,
            token_sha256=token.token_sha256,
            expires_at=None,
            revoked_at=None,
            last_used_at=None,
            client_id=uuid4(),
            client_name="harborline-support-bot",
            client_status=ClientStatus.ACTIVE,
            scopes=frozenset({"echo__say"}),
        )
        stored = replace(stored, **overrides)  # type: ignore[arg-type]
        self.tokens[token.lookup_id] = stored
        return stored

    async def find_token(self, lookup_id: str) -> StoredToken | None:
        return self.tokens.get(lookup_id)

    async def record_token_use(self, token_id: UUID, used_at: datetime) -> None:
        self.uses.append(token_id)


def _tampered(token: str) -> str:
    """The same token with its last character changed: well-formed, wrong secret."""
    return token[:-1] + ("A" if token[-1] != "A" else "B")


@pytest.fixture
def registry() -> InMemoryRegistry:
    return InMemoryRegistry()


@pytest.fixture
def verifier(registry: InMemoryRegistry) -> TokenVerifier:
    return TokenVerifier(registry, clock=lambda: NOW)


@pytest.mark.anyio
async def test_valid_token_verifies(registry: InMemoryRegistry, verifier: TokenVerifier) -> None:
    token = generate_token()
    stored = registry.add(token)

    result = await verifier.verify(token.plaintext)

    assert isinstance(result, VerifiedClient)
    assert result.client_id == stored.client_id
    assert result.scopes == frozenset({"echo__say"})
    assert registry.uses == [stored.token_id]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"revoked_at": NOW - timedelta(seconds=1)}, AuthFailureReason.REVOKED),
        ({"expires_at": NOW}, AuthFailureReason.EXPIRED),
        ({"client_status": ClientStatus.DISABLED}, AuthFailureReason.CLIENT_DISABLED),
    ],
)
async def test_unusable_tokens_fail(
    registry: InMemoryRegistry,
    verifier: TokenVerifier,
    overrides: dict[str, object],
    reason: AuthFailureReason,
) -> None:
    token = generate_token()
    registry.add(token, **overrides)

    assert await verifier.verify(token.plaintext) == AuthFailure(reason, token.lookup_id)
    assert registry.uses == []


@pytest.mark.anyio
async def test_wrong_secret_and_unknown_token_fail(
    registry: InMemoryRegistry, verifier: TokenVerifier
) -> None:
    token = generate_token()
    registry.add(token)
    forged = _tampered(token.plaintext)
    unknown = generate_token()

    assert await verifier.verify(forged) == AuthFailure(
        AuthFailureReason.WRONG_SECRET, token.lookup_id
    )
    assert await verifier.verify(unknown.plaintext) == AuthFailure(
        AuthFailureReason.UNKNOWN_TOKEN, unknown.lookup_id
    )
    assert await verifier.verify("not-a-token") == AuthFailure(AuthFailureReason.MALFORMED)


@pytest.mark.anyio
async def test_token_use_is_recorded_at_most_once_a_minute(
    registry: InMemoryRegistry, verifier: TokenVerifier
) -> None:
    token = generate_token()
    registry.add(token, last_used_at=NOW - timedelta(seconds=30))

    await verifier.verify(token.plaintext)

    assert registry.uses == []


@pytest.mark.anyio
async def test_access_token_carries_the_lookup_id_not_the_secret(
    registry: InMemoryRegistry, verifier: TokenVerifier
) -> None:
    token = generate_token()
    registry.add(token)

    access_token = await verifier.verify_token(token.plaintext)

    assert access_token is not None
    assert access_token.token == token.lookup_id
    assert token.plaintext not in access_token.model_dump_json()


async def _whoami(scope: Scope, receive: Receive, send: Send) -> None:
    user = Request(scope).user
    assert isinstance(user, AuthenticatedUser)
    await JSONResponse({"client_id": user.access_token.client_id})(scope, receive, send)


def _client(verifier: TokenVerifier, events: MemoryEventSink) -> httpx2.AsyncClient:
    app = BearerAuthMiddleware(_whoami, verifier, events)
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1")


@pytest.mark.anyio
async def test_middleware_lets_a_valid_token_through(
    registry: InMemoryRegistry, verifier: TokenVerifier
) -> None:
    token = generate_token()
    stored = registry.add(token)
    events = MemoryEventSink()

    async with _client(verifier, events) as client:
        response = await client.get("/", headers={"Authorization": f"Bearer {token.plaintext}"})

    assert response.status_code == 200
    assert response.json() == {"client_id": str(stored.client_id)}
    assert events.events == []


@pytest.mark.anyio
async def test_middleware_rejects_a_missing_token_without_an_error_code(
    verifier: TokenVerifier,
) -> None:
    events = MemoryEventSink()

    async with _client(verifier, events) as client:
        response = await client.get("/")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="ai-gateway"'
    assert [event.payload["reason"] for event in events.events] == ["missing"]


@pytest.mark.anyio
async def test_every_bad_token_gets_the_same_response(
    registry: InMemoryRegistry, verifier: TokenVerifier
) -> None:
    revoked, expired, disabled, good = (generate_token() for _ in range(4))
    registry.add(revoked, revoked_at=NOW)
    registry.add(expired, expires_at=NOW)
    registry.add(disabled, client_status=ClientStatus.DISABLED)
    registry.add(good)
    presented = [
        revoked.plaintext,
        expired.plaintext,
        disabled.plaintext,
        _tampered(good.plaintext),
        generate_token().plaintext,
        "garbage",
    ]
    events = MemoryEventSink()

    async with _client(verifier, events) as client:
        responses = [
            await client.get("/", headers={"Authorization": f"Bearer {value}"})
            for value in presented
        ]

    assert {response.status_code for response in responses} == {401}
    assert {response.content for response in responses} == {responses[0].content}
    assert {response.headers["www-authenticate"] for response in responses} == {
        'Bearer realm="ai-gateway", error="invalid_token"'
    }
    assert len(events.events) == len(presented)


@pytest.mark.anyio
async def test_raw_tokens_never_reach_logs_or_events(
    registry: InMemoryRegistry,
    verifier: TokenVerifier,
    caplog: pytest.LogCaptureFixture,
) -> None:
    good, revoked = generate_token(), generate_token()
    registry.add(good)
    registry.add(revoked, revoked_at=NOW)
    events = MemoryEventSink()
    caplog.set_level(logging.DEBUG)

    async with _client(verifier, events) as client:
        for token in (good, revoked):
            await client.get("/", headers={"Authorization": f"Bearer {token.plaintext}"})

    recorded = caplog.text + "".join(event.model_dump_json() for event in events.events)
    for token in (good, revoked):
        secret = token.plaintext.split("_", 2)[2]
        assert secret not in recorded
