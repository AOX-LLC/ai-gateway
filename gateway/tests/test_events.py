import pytest
from pydantic import ValidationError

from ai_gateway.seams.events import MAX_PAYLOAD_BYTES, GatewayEvent


@pytest.mark.parametrize("key", ["token", "api_key", "client-secret", "Authorization", "password"])
def test_payload_keys_that_name_secrets_are_rejected(key: str) -> None:
    with pytest.raises(ValidationError, match="look like secrets"):
        GatewayEvent(action="gateway.tool_call", actor_id="client:x", payload={"nested": {key: 1}})


def test_counts_and_lookup_ids_are_allowed() -> None:
    event = GatewayEvent(
        action="gateway.tool_call",
        actor_id="client:x",
        payload={"input_tokens": 3, "lookup_id": "abcdefgh"},
    )

    assert event.payload["lookup_id"] == "abcdefgh"


def test_oversized_payloads_are_rejected() -> None:
    with pytest.raises(ValidationError, match="limit"):
        GatewayEvent(
            action="gateway.tool_call",
            actor_id="client:x",
            payload={"blob": "x" * MAX_PAYLOAD_BYTES},
        )


@pytest.mark.parametrize("actor_id", ["someone@example.com", "", "-leading-dash"])
def test_actor_ids_follow_the_audit_log_format(actor_id: str) -> None:
    with pytest.raises(ValidationError):
        GatewayEvent(action="gateway.tool_call", actor_id=actor_id)
