"""The CRM MCP server of Harborline Supply Co., a fictional company.

Everything here, from the seeded accounts to the contacts, is synthetic. The server is one
of the gateway's upstreams; it is reached only with a service credential, and it only ever
reads: its database role has SELECT on the public columns and nothing else.
"""

MIGRATIONS_PACKAGE = __name__
"""Where the CRM schema's numbered SQL migrations live, for mcp_common.migrate."""

SCHEMA = "crm"
ROLE = "crm_app"
CONNECTION_KWARGS = {"options": f"-c search_path={SCHEMA}"}
"""Connection settings for crm_app: its queries use bare table names in its own schema."""
