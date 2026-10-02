"""Queries against the client registry.

GatewayRegistry is what the running gateway uses; its database role may only read the
registry and record token use. AdminRegistry is for the admin CLI and runs as the owner.
"""

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ai_gateway.registry.models import ClientStatus, StoredToken, UpstreamServer

_SELECT_BY_LOOKUP_ID = """
SELECT t.id AS token_id, t.lookup_id, t.token_sha256, t.expires_at, t.revoked_at,
       t.last_used_at, c.id AS client_id, c.name AS client_name, c.status AS client_status,
       coalesce(array_agg(s.tool) FILTER (WHERE s.tool IS NOT NULL), '{}') AS scopes
FROM client_tokens t
JOIN clients c ON c.id = t.client_id
LEFT JOIN client_scopes s ON s.client_id = c.id
WHERE t.lookup_id = %s
GROUP BY t.id, c.id
"""

_ENABLED_UPSTREAMS = """
SELECT id, namespace, url, connect_timeout_ms, call_timeout_ms, credential_env
FROM upstream_servers
WHERE enabled
ORDER BY namespace
"""


class GatewayRegistry:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def find_token(self, lookup_id: str) -> StoredToken | None:
        async with self._pool.connection() as connection:
            cursor = connection.cursor(row_factory=dict_row)
            await cursor.execute(_SELECT_BY_LOOKUP_ID, (lookup_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return StoredToken(
            token_id=row["token_id"],
            lookup_id=row["lookup_id"],
            token_sha256=bytes(row["token_sha256"]),
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            last_used_at=row["last_used_at"],
            client_id=row["client_id"],
            client_name=row["client_name"],
            client_status=ClientStatus(row["client_status"]),
            scopes=frozenset(row["scopes"]),
        )

    async def record_token_use(self, token_id: UUID, used_at: datetime) -> None:
        async with self._pool.connection() as connection:
            await connection.execute(
                "UPDATE client_tokens SET last_used_at = %s WHERE id = %s", (used_at, token_id)
            )

    async def schema_version(self) -> int:
        """The newest applied migration, or 0 for an empty database."""
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT coalesce(max(version), 0) FROM schema_migrations"
            )
            return _first_column(await cursor.fetchone(), int)

    async def enabled_upstreams(self) -> list[UpstreamServer]:
        async with self._pool.connection() as connection:
            cursor = connection.cursor(row_factory=dict_row)
            await cursor.execute(_ENABLED_UPSTREAMS)
            rows = await cursor.fetchall()
        return [
            UpstreamServer(
                id=row["id"],
                namespace=row["namespace"],
                url=row["url"],
                connect_timeout_s=row["connect_timeout_ms"] / 1000,
                call_timeout_s=row["call_timeout_ms"] / 1000,
                credential_env=row["credential_env"],
            )
            for row in rows
        ]


def _first_column[T](row: tuple[object, ...] | None, expected_type: type[T]) -> T:
    """The first column of a row that the query guarantees exists."""
    if row is None or not isinstance(row[0], expected_type):
        raise RuntimeError(f"expected one {expected_type.__name__} row, got {row!r}")
    return row[0]


class ClientNotFoundError(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no client named {name!r}")


class AdminRegistry:
    """Write operations for the admin CLI. Each method commits its own transaction."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def upsert_client(self, name: str, description: str = "") -> UUID:
        async with self._connection.transaction():
            cursor = await self._connection.execute(
                "INSERT INTO clients (name, description) VALUES (%s, %s)"
                " ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description,"
                " updated_at = now()"
                " RETURNING id",
                (name, description),
            )
            row = await cursor.fetchone()
        return _first_column(row, UUID)

    async def client_id(self, name: str) -> UUID:
        cursor = await self._connection.execute("SELECT id FROM clients WHERE name = %s", (name,))
        row = await cursor.fetchone()
        if row is None:
            raise ClientNotFoundError(name)
        client_id: UUID = row[0]
        return client_id

    async def set_client_status(self, name: str, status: ClientStatus) -> None:
        async with self._connection.transaction():
            cursor = await self._connection.execute(
                "UPDATE clients SET status = %s, updated_at = now() WHERE name = %s",
                (status.value, name),
            )
        if cursor.rowcount == 0:
            raise ClientNotFoundError(name)

    async def grant_scopes(self, client_id: UUID, tools: Iterable[str]) -> None:
        async with self._connection.transaction():
            for tool in tools:
                await self._connection.execute(
                    "INSERT INTO client_scopes (client_id, tool) VALUES (%s, %s)"
                    " ON CONFLICT DO NOTHING",
                    (client_id, tool),
                )

    async def revoke_scopes(self, client_id: UUID, tools: Iterable[str]) -> None:
        async with self._connection.transaction():
            await self._connection.execute(
                "DELETE FROM client_scopes WHERE client_id = %s AND tool = ANY(%s)",
                (client_id, list(tools)),
            )

    async def count_live_tokens(self, client_id: UUID) -> int:
        cursor = await self._connection.execute(
            "SELECT count(*) FROM client_tokens WHERE client_id = %s AND revoked_at IS NULL"
            " AND (expires_at IS NULL OR expires_at > now())",
            (client_id,),
        )
        return _first_column(await cursor.fetchone(), int)

    async def insert_token(
        self,
        client_id: UUID,
        lookup_id: str,
        token_sha256: bytes,
        label: str,
        expires_at: datetime | None,
    ) -> None:
        async with self._connection.transaction():
            await self._connection.execute(
                "INSERT INTO client_tokens (client_id, lookup_id, token_sha256, label, expires_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (client_id, lookup_id, token_sha256, label, expires_at),
            )

    async def expire_live_tokens(
        self, client_id: UUID, expires_at: datetime, keep_lookup_id: str
    ) -> int:
        """Bring forward the expiry of the client's other live tokens; return how many changed."""
        async with self._connection.transaction():
            cursor = await self._connection.execute(
                "UPDATE client_tokens SET expires_at = %s"
                " WHERE client_id = %s AND lookup_id <> %s AND revoked_at IS NULL"
                " AND (expires_at IS NULL OR expires_at > %s)",
                (expires_at, client_id, keep_lookup_id, expires_at),
            )
        return cursor.rowcount

    async def revoke_token(self, lookup_id: str) -> bool:
        async with self._connection.transaction():
            cursor = await self._connection.execute(
                "UPDATE client_tokens SET revoked_at = now()"
                " WHERE lookup_id = %s AND revoked_at IS NULL",
                (lookup_id,),
            )
        return cursor.rowcount == 1

    async def revoke_all_tokens(self, client_id: UUID) -> int:
        async with self._connection.transaction():
            cursor = await self._connection.execute(
                "UPDATE client_tokens SET revoked_at = now()"
                " WHERE client_id = %s AND revoked_at IS NULL",
                (client_id,),
            )
        return cursor.rowcount

    async def upsert_upstream(
        self,
        namespace: str,
        url: str,
        connect_timeout_ms: int,
        call_timeout_ms: int,
        credential_env: str | None = None,
    ) -> None:
        async with self._connection.transaction():
            await self._connection.execute(
                "INSERT INTO upstream_servers"
                " (namespace, url, connect_timeout_ms, call_timeout_ms, credential_env)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (namespace) DO UPDATE SET url = EXCLUDED.url,"
                " connect_timeout_ms = EXCLUDED.connect_timeout_ms,"
                " call_timeout_ms = EXCLUDED.call_timeout_ms,"
                " credential_env = EXCLUDED.credential_env, enabled = true, updated_at = now()",
                (namespace, url, connect_timeout_ms, call_timeout_ms, credential_env),
            )
