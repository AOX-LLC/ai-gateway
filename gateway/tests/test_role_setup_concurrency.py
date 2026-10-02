"""Two setups run side by side in Compose, and both change the database-level access list."""

from uuid import uuid4

import anyio
import psycopg
import pytest
from psycopg import sql

from mcp_common.roles import ensure_role, restrict_database_access

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROUNDS = 25


async def _setup_one(url: str, role: str, failures: list[BaseException]) -> None:
    try:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
            for _ in range(ROUNDS):
                await ensure_role(connection, "pw-" + uuid4().hex, role)
                await restrict_database_access(connection, [role])
    except BaseException as error:
        failures.append(error)


async def test_two_setups_changing_the_database_access_list_at_once_do_not_collide(
    test_database_url: str,
) -> None:
    roles = [f"scratch_a_{uuid4().hex[:8]}", f"scratch_b_{uuid4().hex[:8]}"]
    failures: list[BaseException] = []
    try:
        async with anyio.create_task_group() as tasks:
            for role in roles:
                tasks.start_soon(_setup_one, test_database_url, role, failures)
    finally:
        async with await psycopg.AsyncConnection.connect(
            test_database_url, autocommit=True
        ) as connection:
            for role in roles:
                await connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                await connection.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                )

    assert failures == []
