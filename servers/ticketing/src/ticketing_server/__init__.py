"""The ticketing MCP server of Harborline Supply Co., a fictional company.

Everything here, from the seeded tickets to the staff names, is synthetic. The server is
one of the gateway's upstreams; it is reached only with a service credential and records
which gateway client asked, for attribution and never for authorization.
"""

MIGRATIONS_PACKAGE = __name__
"""Where the ticketing schema's numbered SQL migrations live, for mcp_common.migrate."""

SCHEMA = "ticketing"
ROLE = "ticketing_app"
CONNECTION_KWARGS = {"options": f"-c search_path={SCHEMA}"}
"""Connection settings for ticketing_app: its queries use bare table names in its own schema."""
