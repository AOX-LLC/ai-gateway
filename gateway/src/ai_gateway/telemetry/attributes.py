"""Span and attribute names. The Phase 5 dashboard will depend on them, so they are public API.

The gateway only uses the OpenTelemetry API, which records nothing until an application
configures the SDK and an exporter. No span ever carries a credential, a header, tool
arguments or tool results.
"""

SPAN_TOOLS_LIST = "gateway.tools_list"
SPAN_TOOL_CALL = "gateway.tool_call"
SPAN_UPSTREAM_CALL = "gateway.upstream.call"
SPAN_LAYER_PREFIX = "gateway.layer."

GATEWAY_TOOL = "gateway.tool"
GATEWAY_REQUEST_ID = "gateway.request_id"
GATEWAY_CLIENT = "gateway.client"
