"""The handbook search MCP server of Harborline Supply Co., a fictional company.

Every document here is synthetic. The server answers hybrid (meaning plus keyword) search
over the employee handbook and returns documents by id. Restricted documents are
unreachable by design: the server's database role can read only two views that leave them
out, so even a query bug cannot return one. It is one of the gateway's upstreams, reached
only with a service credential.
"""

MIGRATIONS_PACKAGE = __name__
"""Where the handbook schema's numbered SQL migrations live, for mcp_common.migrate."""

SCHEMA = "handbook"
ROLE = "handbook_app"
CONNECTION_KWARGS = {"options": f"-c search_path={SCHEMA}"}
"""Connection settings for handbook_app: its queries use bare names in its own schema, where
the pgvector extension also lives."""

EMBEDDING_DIM = 256
"""The width of the embedding vectors: potion-base-8M's dimension, and the column's."""
