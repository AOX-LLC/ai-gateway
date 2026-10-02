"""Smoke test for the database fixtures."""

from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.registry.repo import AdminRegistry


@pytest.mark.integration
@pytest.mark.anyio
async def test_make_client_stores_token(
    make_client: Callable[..., Awaitable[tuple[UUID, IssuedToken]]],
    db_pool: AsyncConnectionPool,
    admin_registry: AdminRegistry,
) -> None:
    client_id, token = await make_client("harbor-test", ["echo__say"])

    async with db_pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT client_id FROM client_tokens WHERE lookup_id = %s", (token.lookup_id,)
        )
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == client_id
    assert await admin_registry.count_live_tokens(client_id) == 1
