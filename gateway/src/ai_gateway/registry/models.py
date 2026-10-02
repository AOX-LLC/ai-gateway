"""Rows the gateway reads from the client registry."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ClientStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True)
class StoredToken:
    """A token row joined with its client and the client's scopes."""

    token_id: UUID
    lookup_id: str
    token_sha256: bytes = field(repr=False)
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    client_id: UUID
    client_name: str
    client_status: ClientStatus
    scopes: frozenset[str]


@dataclass(frozen=True)
class UpstreamServer:
    id: UUID
    namespace: str
    url: str
    connect_timeout_s: float
    call_timeout_s: float
    credential_env: str | None = None
    """Name of the environment variable holding the credential to send, never its value."""
