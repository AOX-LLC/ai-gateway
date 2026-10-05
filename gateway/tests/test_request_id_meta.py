"""The request id in a result's `_meta`: every kind of result, and an upstream cannot forge it."""

from uuid import uuid4

from mcp.types import CallToolResult, TextContent

from ai_gateway.pipeline.runner import UpstreamStatus
from ai_gateway.pipeline.types import Deny, DenyCode, Disposition
from ai_gateway.proxy.server import (
    POLICY_BLOCKED,
    REQUEST_ID_META_KEY,
    _deny_error,
    _internal_error,
    _pending_result,
    _upstream_failure,
    _with_request_id,
)
from ai_gateway.proxy.sessions import UpstreamCallError


def _text(text: str, **fields: object) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], **fields)  # type: ignore[arg-type]


def test_a_successful_result_gets_the_id_and_keeps_its_content() -> None:
    request_id = uuid4()

    result = _with_request_id(_text("dock 7 is clear"), request_id)

    assert result.meta == {REQUEST_ID_META_KEY: str(request_id)}
    assert result.content == [TextContent(type="text", text="dock 7 is clear")]
    assert result.is_error is False


def test_a_tools_own_error_result_gets_it_too() -> None:
    request_id = uuid4()

    result = _with_request_id(_text("no such ticket", is_error=True), request_id)

    assert result.is_error is True
    assert result.meta == {REQUEST_ID_META_KEY: str(request_id)}


def test_a_pending_write_gets_it_beside_its_approval_id() -> None:
    request_id = uuid4()
    deny = Deny(DenyCode.APPROVAL_PENDING, "Needs approval.", Disposition.PENDING, "appr-1")

    result = _with_request_id(_pending_result(deny), request_id)

    assert result.is_error is True
    assert result.meta == {REQUEST_ID_META_KEY: str(request_id)}
    assert result.structured_content is not None
    assert result.structured_content["approval_id"] == "appr-1"


def test_a_failed_upstream_result_gets_it() -> None:
    request_id = uuid4()
    failure = _upstream_failure(UpstreamCallError(UpstreamStatus.TIMEOUT, "echo"), request_id)

    result = _with_request_id(failure, request_id)

    assert result.meta == {REQUEST_ID_META_KEY: str(request_id)}


def test_an_upstream_cannot_choose_the_id_a_client_reads() -> None:
    request_id = uuid4()
    forged = _text("ok", _meta={REQUEST_ID_META_KEY: "forged", "other": 1})

    result = _with_request_id(forged, request_id)

    assert result.meta == {REQUEST_ID_META_KEY: str(request_id), "other": 1}


def test_every_error_the_gateway_answers_a_call_with_carries_the_id() -> None:
    request_id = uuid4()
    expected = {"request_id": str(request_id), "_meta": {REQUEST_ID_META_KEY: str(request_id)}}

    refused = _deny_error(Deny(DenyCode.RATE_LIMITED, "Slow down."), request_id)
    unavailable = _deny_error(Deny(DenyCode.TOOL_UNAVAILABLE, "Not available."), request_id)

    assert refused.code == POLICY_BLOCKED
    assert refused.data == expected
    assert unavailable.data == expected
    assert _internal_error(request_id).data == expected
