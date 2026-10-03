"""reset_role takes back every membership of a role, whoever granted it."""

import psycopg
import pytest

from mcp_common.roles import reset_role

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def _drop(connection: psycopg.AsyncConnection) -> None:
    for role in ("reset_target", "reset_grantor"):
        cursor = await connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if await cursor.fetchone():
            await connection.execute(f"DROP OWNED BY {role}".encode())
            await connection.execute(f"DROP ROLE {role}".encode())


async def test_a_membership_granted_by_another_role_is_revoked_and_the_check_is_made_again(
    test_database_url: str,
) -> None:
    async with await psycopg.AsyncConnection.connect(
        test_database_url, autocommit=True
    ) as connection:
        await _drop(connection)
        try:
            await connection.execute("CREATE ROLE reset_target NOLOGIN")
            await connection.execute("CREATE ROLE reset_grantor NOLOGIN CREATEROLE")
            await connection.execute("GRANT pg_read_all_data TO reset_grantor WITH ADMIN OPTION")
            await connection.execute(
                "GRANT pg_read_all_data TO reset_target GRANTED BY reset_grantor"
            )
            await connection.execute("GRANT pg_monitor TO reset_target")  # granted by the owner

            await reset_role(connection, "reset_target")

            cursor = await connection.execute(
                "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member"
                " WHERE r.rolname = 'reset_target'"
            )
            assert await cursor.fetchone() == (0,)
        finally:
            await _drop(connection)
