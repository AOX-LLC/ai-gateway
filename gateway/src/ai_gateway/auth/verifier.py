"""Verify a presented bearer token against the client registry.

Every failure looks the same to the caller. The reason is kept for the security event
only, so a client cannot learn whether a token exists, was revoked, or has expired.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from mcp.server.auth.provider import AccessToken
from psycopg import Error as DatabaseError

from ai_gateway.auth.tokens import parse_lookup_id, token_matches
from ai_gateway.registry.models import ClientStatus, StoredToken

logger = logging.getLogger(__name__)

TOKEN_ISSUER = "ai-gateway"  # noqa: S105 - an issuer name, not a secret

# Compared against when no row matches, so an unknown lookup id costs the same
# constant-time comparison as a known one with the wrong secret.
_UNMATCHABLE_SHA256 = bytes(32)

# Recording every use would turn each authenticated request into a write.
_TOKEN_USE_RECORD_INTERVAL = timedelta(minutes=1)


class TokenRegistry(Protocol):
    async def find_token(self, lookup_id: str) -> StoredToken | None: ...

    async def record_token_use(self, token_id: UUID, used_at: datetime) -> None: ...


class AuthFailureReason(StrEnum):
    MISSING = "missing"
    MALFORMED = "malformed"
    UNKNOWN_TOKEN = "unknown_token"  # noqa: S105 - a reason code
    WRONG_SECRET = "wrong_secret"  # noqa: S105 - a reason code
    REVOKED = "revoked"
    EXPIRED = "expired"
    CLIENT_DISABLED = "client_disabled"


@dataclass(frozen=True)
class AuthFailure:
    reason: AuthFailureReason
    lookup_id: str | None = None


@dataclass(frozen=True)
class VerifiedClient:
    client_id: UUID
    client_name: str
    lookup_id: str
    scopes: frozenset[str]

    def to_access_token(self) -> AccessToken:
        """Build the SDK's AccessToken. It carries the lookup id, never the raw token,
        so the secret is not kept in the request context."""
        return AccessToken(
            token=self.lookup_id,
            client_id=str(self.client_id),
            scopes=sorted(self.scopes),
            subject=str(self.client_id),
            claims={"iss": TOKEN_ISSUER, "client_name": self.client_name},
        )


class TokenVerifier:
    def __init__(
        self,
        registry: TokenRegistry,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._registry = registry
        self._clock = clock

    async def verify(self, presented: str) -> VerifiedClient | AuthFailure:
        lookup_id = parse_lookup_id(presented)
        if lookup_id is None:
            return AuthFailure(AuthFailureReason.MALFORMED)

        stored = await self._registry.find_token(lookup_id)
        secret_matches = token_matches(
            presented, stored.token_sha256 if stored else _UNMATCHABLE_SHA256
        )
        if stored is None:
            return AuthFailure(AuthFailureReason.UNKNOWN_TOKEN, lookup_id)
        if not secret_matches:
            return AuthFailure(AuthFailureReason.WRONG_SECRET, lookup_id)

        now = self._clock()
        if stored.revoked_at is not None:
            return AuthFailure(AuthFailureReason.REVOKED, lookup_id)
        if stored.expires_at is not None and stored.expires_at <= now:
            return AuthFailure(AuthFailureReason.EXPIRED, lookup_id)
        if stored.client_status is not ClientStatus.ACTIVE:
            return AuthFailure(AuthFailureReason.CLIENT_DISABLED, lookup_id)

        await self._record_use(stored, now)
        return VerifiedClient(
            client_id=stored.client_id,
            client_name=stored.client_name,
            lookup_id=lookup_id,
            scopes=stored.scopes,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """The MCP SDK's TokenVerifier protocol."""
        result = await self.verify(token)
        return result.to_access_token() if isinstance(result, VerifiedClient) else None

    async def _record_use(self, stored: StoredToken, now: datetime) -> None:
        recently_recorded = (
            stored.last_used_at is not None
            and now - stored.last_used_at < _TOKEN_USE_RECORD_INTERVAL
        )
        if recently_recorded:
            return
        try:
            await self._registry.record_token_use(stored.token_id, now)
        except DatabaseError:
            # Bookkeeping only: a failed write must not turn a valid token into a 401.
            logger.warning("could not record use of token %s", stored.lookup_id, exc_info=True)
