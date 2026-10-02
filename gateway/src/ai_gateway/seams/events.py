"""Gateway events: one per authentication failure, tools/list and tools/call.

The event's shape follows the pre-release AuditEvent of the shared agent-core library,
so a later phase can append these to its hash-chained audit log unchanged. That
interface is not yet released and may shift before v0.1.0. Until then events go to
the structured log.

A payload holds ids, hashes, counts and reason codes. It never holds credentials,
headers, tool arguments or tool results.
"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator

MAX_PAYLOAD_BYTES = 8_192

# The audit log rejects keys that look like they name a secret. Checking here as well
# means a bad key fails in this repo's tests, not after the audit log is wired in.
_SECRET_LOOKING_KEY = re.compile(
    r"(apikey|authorization|cookie|credentials?|passwd|password|privatekey|secret|token)$"
)

ActionName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$", max_length=100)]
ActorId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class GatewayEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: ActionName
    actor_id: ActorId
    subject_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    occurred_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("payload")
    @classmethod
    def _payload_is_small_and_names_no_secret(
        cls, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        secret_looking = sorted(_secret_looking_keys(payload))
        if secret_looking:
            raise ValueError(f"payload keys look like secrets: {', '.join(secret_looking)}")

        size = len(json.dumps(payload, separators=(",", ":")).encode())
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload is {size} bytes; the limit is {MAX_PAYLOAD_BYTES}")
        return payload


class EventSink(Protocol):
    async def emit(self, event: GatewayEvent) -> None: ...


class LogEventSink:
    """Writes each event as one JSON line on the `ai_gateway.events` logger."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("ai_gateway.events")

    async def emit(self, event: GatewayEvent) -> None:
        self._logger.info(event.model_dump_json())


class MemoryEventSink:
    """Keeps events in memory, for tests."""

    def __init__(self) -> None:
        self.events: list[GatewayEvent] = []

    async def emit(self, event: GatewayEvent) -> None:
        self.events.append(event)


def _secret_looking_keys(value: JsonValue) -> set[str]:
    if isinstance(value, list):
        return set().union(*(_secret_looking_keys(item) for item in value))
    if not isinstance(value, dict):
        return set()

    found = {key for key in value if _SECRET_LOOKING_KEY.search(_normalize_key(key))}
    for nested in value.values():
        found |= _secret_looking_keys(nested)
    return found


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())
