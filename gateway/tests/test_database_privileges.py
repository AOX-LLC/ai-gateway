"""The gateway's database role may read the registry and record token use, nothing more."""

from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

import psycopg
import pytest
from psycopg import AsyncConnection

from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.registry.repo import AdminRegistry

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MakeClient = Callable[..., Awaitable[tuple[UUID, IssuedToken]]]

READABLE_TABLES = [
    "clients",
    "client_tokens",
    "client_scopes",
    "upstream_servers",
    "tool_policies",
    "schema_migrations",
]

FORBIDDEN_WRITES = {
    "insert client": "INSERT INTO clients (name) VALUES ('intruder')",
    "insert token": (
        "INSERT INTO client_tokens (client_id, lookup_id, token_sha256)"
        " SELECT id, 'abcdefgh', sha256('x') FROM clients LIMIT 1"
    ),
    "insert scope": (
        "INSERT INTO client_scopes (client_id, tool) SELECT id, 'echo__shout' FROM clients LIMIT 1"
    ),
    "insert upstream": "INSERT INTO upstream_servers (namespace, url) VALUES ('evil', 'http://x')",
    "insert tool policy": (
        "INSERT INTO tool_policies (namespace, tool, effect) VALUES ('echo', 'shout', 'read')"
    ),
    "update tool policy": "UPDATE tool_policies SET effect = 'read'",
    "delete tool policy": "DELETE FROM tool_policies",
    "insert migration": "INSERT INTO schema_migrations (version) VALUES (999)",
    "update client status": "UPDATE clients SET status = 'active'",
    "update scope": "UPDATE client_scopes SET tool = 'echo__shout'",
    "update upstream url": "UPDATE upstream_servers SET url = 'http://attacker.test/mcp'",
    "update token revoked_at": "UPDATE client_tokens SET revoked_at = NULL",
    "update token expires_at": "UPDATE client_tokens SET expires_at = NULL",
    "update token hash": "UPDATE client_tokens SET token_sha256 = sha256('x')",
    "update migration": "UPDATE schema_migrations SET version = version",
    "delete client": "DELETE FROM clients",
    "delete token": "DELETE FROM client_tokens",
    "delete scope": "DELETE FROM client_scopes",
    "delete upstream": "DELETE FROM upstream_servers",
    "delete migration": "DELETE FROM schema_migrations",
    **{f"truncate {table}": f"TRUNCATE {table} CASCADE" for table in READABLE_TABLES},
    "create table": "CREATE TABLE intruder (id integer)",
}


@pytest.fixture
async def seeded_lookup_id(make_client: MakeClient, admin_registry: AdminRegistry) -> str:
    await admin_registry.upsert_upstream("echo", "http://echo.test/mcp", 5000, 30000)
    _, token = await make_client("harborline-support-bot", ["echo__say"])
    return token.lookup_id


@pytest.fixture
async def as_gateway(
    app_database_url: str, seeded_lookup_id: str
) -> AsyncIterator[AsyncConnection]:
    connection = await AsyncConnection.connect(app_database_url, autocommit=True)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.parametrize("table", READABLE_TABLES)
async def test_the_gateway_role_reads_the_registry(as_gateway: AsyncConnection, table: str) -> None:
    query = f"SELECT count(*) FROM {table}"  # noqa: S608 - table names come from the list above
    cursor = await as_gateway.execute(query.encode())

    assert await cursor.fetchone() is not None


@pytest.mark.parametrize("statement", FORBIDDEN_WRITES.values(), ids=FORBIDDEN_WRITES.keys())
async def test_the_gateway_role_cannot_change_the_registry(
    as_gateway: AsyncConnection, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_gateway.execute(statement.encode())


async def test_the_gateway_role_may_record_token_use(
    as_gateway: AsyncConnection, seeded_lookup_id: str
) -> None:
    cursor = await as_gateway.execute(
        "UPDATE client_tokens SET last_used_at = now() WHERE lookup_id = %s", (seeded_lookup_id,)
    )

    assert cursor.rowcount == 1


# --- the ticketing server's role and schema -------------------------------------------------

TICKETING_READABLE = ["staff", "tickets", "comments", "schema_migrations"]

TICKETING_FORBIDDEN = {
    "update ticket subject": "UPDATE ticketing.tickets SET subject = 'rewritten'",
    "update ticket description": "UPDATE ticketing.tickets SET description = 'rewritten'",
    "update ticket internal notes": "UPDATE ticketing.tickets SET internal_notes = NULL",
    "update ticket requested_by": "UPDATE ticketing.tickets SET requested_by = 'someone-else'",
    "update ticket account": "UPDATE ticketing.tickets SET account_id = 'ACC-00001'",
    "update ticket id": "UPDATE ticketing.tickets SET id = 'TKT-999999'",
    "update ticket created_at": "UPDATE ticketing.tickets SET created_at = now()",
    "update comment body": "UPDATE ticketing.comments SET body = 'rewritten'",
    "update comment visibility": "UPDATE ticketing.comments SET visibility = 'public'",
    "update staff": "UPDATE ticketing.staff SET team = 'nowhere'",
    "insert staff": (
        "INSERT INTO ticketing.staff (handle, display_name, team) VALUES ('a.b', 'A', 't')"
    ),
    "delete ticket": "DELETE FROM ticketing.tickets",
    "delete comment": "DELETE FROM ticketing.comments",
    "delete staff": "DELETE FROM ticketing.staff",
    "truncate tickets": "TRUNCATE ticketing.tickets CASCADE",
    "truncate comments": "TRUNCATE ticketing.comments",
    "create table in ticketing": "CREATE TABLE ticketing.intruder (id integer)",
    "create table in public": "CREATE TABLE public.intruder (id integer)",
    "drop table": "DROP TABLE ticketing.comments",
    "insert ticket internal notes": (
        "INSERT INTO ticketing.tickets (account_id, subject, description, requested_by,"
        " internal_notes) VALUES ('ACC-00001', 'abc', 'x', 'y', 'planted note')"
    ),
    "insert ticket with a chosen id": (
        "INSERT INTO ticketing.tickets (id, account_id, subject, description, requested_by)"
        " VALUES ('TKT-999999', 'ACC-00001', 'abc', 'x', 'y')"
    ),
    "insert ticket with a chosen status": (
        "INSERT INTO ticketing.tickets (account_id, subject, description, requested_by, status)"
        " VALUES ('ACC-00001', 'abc', 'x', 'y', 'closed')"
    ),
    "insert an internal comment": (
        "INSERT INTO ticketing.comments (ticket_id, author, body, requested_by, visibility)"
        " VALUES ('TKT-000001', 'a.b', 'x', 'y', 'internal')"
    ),
    "insert a comment with a chosen visibility": (
        "INSERT INTO ticketing.comments (ticket_id, author, body, requested_by, visibility)"
        " VALUES ('TKT-000001', 'a.b', 'x', 'y', 'public')"
    ),
    "create a temporary table": "CREATE TEMPORARY TABLE scratch (id integer)",
    "write migrations": "INSERT INTO ticketing.schema_migrations (version) VALUES (999)",
    "update migrations": "UPDATE ticketing.schema_migrations SET version = version",
    "delete migrations": "DELETE FROM ticketing.schema_migrations",
    **{
        f"read registry {table}": f"SELECT count(*) FROM public.{table}"  # noqa: S608 - names come from the list above
        for table in READABLE_TABLES
    },
}


@pytest.fixture
async def as_ticketing(
    ticketing_app_url: str, ticketing_data: object
) -> AsyncIterator[AsyncConnection]:
    connection = await AsyncConnection.connect(ticketing_app_url, autocommit=True)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.parametrize("table", TICKETING_READABLE)
async def test_the_ticketing_role_reads_its_tables(
    as_ticketing: AsyncConnection, table: str
) -> None:
    query = f"SELECT count(*) FROM ticketing.{table}"  # noqa: S608 - names come from the list above
    cursor = await as_ticketing.execute(query.encode())

    assert await cursor.fetchone() is not None


@pytest.mark.parametrize("statement", TICKETING_FORBIDDEN.values(), ids=TICKETING_FORBIDDEN.keys())
async def test_the_ticketing_role_is_confined_to_narrow_writes(
    as_ticketing: AsyncConnection, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_ticketing.execute(statement.encode())


async def test_the_ticketing_role_may_change_status_assignee_and_update_time(
    as_ticketing: AsyncConnection,
) -> None:
    cursor = await as_ticketing.execute(
        "UPDATE ticketing.tickets SET status = 'closed', assignee = NULL, updated_at = now()"
        " WHERE id = 'TKT-000001'"
    )

    assert cursor.rowcount == 1


@pytest.mark.parametrize("table", TICKETING_READABLE)
async def test_the_gateway_role_cannot_see_the_ticketing_schema(
    as_gateway: AsyncConnection, ticketing_data: object, table: str
) -> None:
    query = f"SELECT count(*) FROM ticketing.{table}"  # noqa: S608 - names come from the list above
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_gateway.execute(query.encode())


async def test_the_gateway_role_cannot_write_the_ticketing_schema(
    as_gateway: AsyncConnection, ticketing_data: object
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_gateway.execute(
            b"INSERT INTO ticketing.tickets (account_id, subject, description, requested_by)"
            b" VALUES ('ACC-00001', 'abc', 'x', 'y')"
        )


async def test_the_ticketing_role_cannot_use_the_public_schema(
    as_ticketing: AsyncConnection,
) -> None:
    cursor = await as_ticketing.execute(
        "SELECT has_schema_privilege('public', 'USAGE'), has_schema_privilege('public', 'CREATE'),"
        " has_database_privilege(current_database(), 'TEMPORARY')"
    )

    assert await cursor.fetchone() == (False, False, False)


async def test_a_comment_inserted_without_a_visibility_is_public(
    as_ticketing: AsyncConnection,
) -> None:
    cursor = await as_ticketing.execute(
        "INSERT INTO ticketing.comments (ticket_id, author, body, requested_by)"
        " VALUES ('TKT-000001', 'a.b', 'x', 'y') RETURNING visibility"
    )

    assert await cursor.fetchone() == ("public",)


async def test_only_the_named_roles_may_connect_and_use_the_public_schema(
    as_gateway: AsyncConnection,
) -> None:
    cursor = await as_gateway.execute(
        "SELECT has_database_privilege('gateway_app', current_database(), 'CONNECT'),"
        " has_schema_privilege('gateway_app', 'public', 'USAGE'),"
        " has_database_privilege('ticketing_app', current_database(), 'CONNECT'),"
        " has_database_privilege('gateway_app', current_database(), 'TEMPORARY'),"
        " has_database_privilege(0::oid, current_database(), 'CONNECT'),"
        " has_schema_privilege(0::oid, 'public', 'USAGE')"
    )

    assert await cursor.fetchone() == (True, True, True, False, False, False)


# --- the CRM server's role and schema --------------------------------------------------------

CRM_READABLE = {
    "accounts": "id, name, industry, region, tier, about, account_manager, created_at",
    "contacts": "id, account_id, full_name, title, email, phone",
    "deals": "id, account_id, name, stage, amount_cents, close_date, owner",
    "activity_notes": "id, account_id, deal_id, author, kind, body, occurred_at",
    "schema_migrations": "version, applied_at",
}

# Everything below this line is read from a schema the role must not see at all.
OTHER_SCHEMAS_FORBIDDEN = {
    **{
        f"read registry {table}": f"SELECT count(*) FROM public.{table}"  # noqa: S608 - from the list
        for table in READABLE_TABLES
    },
    "read ticketing tickets": "SELECT count(*) FROM ticketing.tickets",
    "read ticketing comments": "SELECT count(*) FROM ticketing.comments",
    "read ticketing migrations": "SELECT count(*) FROM ticketing.schema_migrations",
}

CRM_FORBIDDEN = {
    "read the credit limit": "SELECT credit_limit_internal FROM crm.accounts",
    "read the risk rating": "SELECT risk_rating_internal FROM crm.accounts",
    "read internal notes": "SELECT internal_notes FROM crm.accounts",
    "read the floor price": "SELECT floor_price_cents FROM crm.deals",
    "read every account column": "SELECT * FROM crm.accounts",
    "read every deal column": "SELECT * FROM crm.deals",
    "read a row as a whole": "SELECT a FROM crm.accounts AS a",
    "filter on an internal column": "SELECT id FROM crm.accounts WHERE internal_notes IS NOT NULL",
    "order by an internal column": "SELECT id FROM crm.deals ORDER BY floor_price_cents",
    "insert an account": (
        "INSERT INTO crm.accounts (id, name, industry, region, tier, about, account_manager,"
        " created_at, credit_limit_internal, risk_rating_internal, internal_notes)"
        " VALUES ('ACC-90000', 'x', 'x', 'x', 'gold', 'x', 'a.b', now(), 1, 'x', 'x')"
    ),
    "update an account name": "UPDATE crm.accounts SET name = 'rewritten'",
    "update a deal amount": "UPDATE crm.deals SET amount_cents = 1",
    "update a note": "UPDATE crm.activity_notes SET body = 'rewritten'",
    "insert a contact": (
        "INSERT INTO crm.contacts (id, account_id, full_name, title, email, phone)"
        " VALUES ('CON-90000', 'ACC-00001', 'x', 'x', 'x@x.example', '555-0100')"
    ),
    "insert a note": (
        "INSERT INTO crm.activity_notes (account_id, author, kind, body, occurred_at)"
        " VALUES ('ACC-00001', 'a.b', 'note', 'x', now())"
    ),
    "delete an account": "DELETE FROM crm.accounts",
    "delete a deal": "DELETE FROM crm.deals",
    "truncate accounts": "TRUNCATE crm.accounts CASCADE",
    "truncate notes": "TRUNCATE crm.activity_notes",
    "write migrations": "INSERT INTO crm.schema_migrations (version) VALUES (999)",
    "update migrations": "UPDATE crm.schema_migrations SET version = version",
    "delete migrations": "DELETE FROM crm.schema_migrations",
    "drop a table": "DROP TABLE crm.contacts",
    "alter a table": "ALTER TABLE crm.accounts ADD COLUMN extra text",
    "create a table in crm": "CREATE TABLE crm.intruder (id integer)",
    "create a table in public": "CREATE TABLE public.intruder (id integer)",
    "create a temporary table": "CREATE TEMPORARY TABLE scratch (id integer)",
    "grant itself more": "GRANT ALL ON crm.accounts TO crm_app",
    "read the handbook views": "SELECT count(*) FROM handbook.published_documents",
    "read the handbook chunks": "SELECT count(*) FROM handbook.searchable_chunks",
    "read the handbook tables": "SELECT count(*) FROM handbook.documents",
    **OTHER_SCHEMAS_FORBIDDEN,
}


@pytest.fixture
async def as_crm(
    crm_app_url: str, ticketing_data: object, crm_data: object, handbook_data: object
) -> AsyncIterator[AsyncConnection]:
    # All three schemas must exist: the statements below reach into the other two, and
    # a missing table would raise UndefinedTable instead of the privilege error expected.
    connection = await AsyncConnection.connect(crm_app_url, autocommit=True)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.parametrize("table", CRM_READABLE)
async def test_the_crm_role_reads_the_public_columns_of_its_tables(
    as_crm: AsyncConnection, table: str
) -> None:
    query = f"SELECT {CRM_READABLE[table]} FROM crm.{table} LIMIT 1"  # noqa: S608 - from the dict above
    cursor = await as_crm.execute(query.encode())

    assert await cursor.fetchone() is not None


@pytest.mark.parametrize("statement", CRM_FORBIDDEN.values(), ids=CRM_FORBIDDEN.keys())
async def test_the_crm_role_is_read_only_and_cannot_see_the_internal_columns(
    as_crm: AsyncConnection, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_crm.execute(statement.encode())


async def test_the_crm_role_has_nothing_in_the_public_schema(as_crm: AsyncConnection) -> None:
    cursor = await as_crm.execute(
        "SELECT has_schema_privilege('public', 'USAGE'), has_schema_privilege('public', 'CREATE'),"
        " has_database_privilege(current_database(), 'TEMPORARY'),"
        " has_schema_privilege('crm', 'CREATE')"
    )

    assert await cursor.fetchone() == (False, False, False, False)


# --- the handbook server's role and schema ----------------------------------------------------

HANDBOOK_READABLE = ["published_documents", "searchable_chunks", "schema_migrations"]

HANDBOOK_FORBIDDEN = {
    "read the documents table": "SELECT count(*) FROM handbook.documents",
    "read the chunks table": "SELECT count(*) FROM handbook.chunks",
    "read restricted text from the documents table": (
        "SELECT body FROM handbook.documents WHERE classification = 'restricted'"
    ),
    "read restricted text from the chunks table": (
        "SELECT c.text FROM handbook.chunks c JOIN handbook.documents d ON d.id = c.document_id"
        " WHERE d.classification = 'restricted'"
    ),
    "read a table as a whole": "SELECT d FROM handbook.documents AS d",
    "insert through a view": (
        "INSERT INTO handbook.published_documents (id, title, category, classification, updated,"
        " body) VALUES ('DOC-900', 'x', 'hr', 'general', now(), 'x')"
    ),
    "update through a view": "UPDATE handbook.published_documents SET body = 'rewritten'",
    "delete through a view": "DELETE FROM handbook.published_documents",
    "insert a chunk": (
        "INSERT INTO handbook.chunks (document_id, ordinal, heading, text, embedding)"
        " SELECT 'DOC-001', 99, 'x', 'x', embedding FROM handbook.searchable_chunks LIMIT 1"
    ),
    "insert a document": (
        "INSERT INTO handbook.documents (id, title, category, classification, updated, body)"
        " VALUES ('DOC-900', 'x', 'hr', 'general', now(), 'x')"
    ),
    "reclassify a document": "UPDATE handbook.documents SET classification = 'general'",
    "truncate documents": "TRUNCATE handbook.documents CASCADE",
    "truncate chunks": "TRUNCATE handbook.chunks",
    "write migrations": "INSERT INTO handbook.schema_migrations (version) VALUES (999)",
    "update migrations": "UPDATE handbook.schema_migrations SET version = version",
    "delete migrations": "DELETE FROM handbook.schema_migrations",
    "drop a view": "DROP VIEW handbook.published_documents",
    "replace a view": (
        "CREATE OR REPLACE VIEW handbook.published_documents AS"
        " SELECT id, title, category, classification, updated, body FROM handbook.documents"
    ),
    "create a table in handbook": "CREATE TABLE handbook.intruder (id integer)",
    "create a function in handbook": (
        "CREATE FUNCTION handbook.leak() RETURNS boolean LANGUAGE sql AS 'SELECT true'"
    ),
    "create a table in public": "CREATE TABLE public.intruder (id integer)",
    "create a temporary table": "CREATE TEMPORARY TABLE scratch (id integer)",
    "create an extension": "CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA handbook",
    "grant itself more": "GRANT ALL ON handbook.documents TO handbook_app",
    "read crm accounts": "SELECT count(*) FROM crm.accounts",
    "read crm contacts": "SELECT count(*) FROM crm.contacts",
    **OTHER_SCHEMAS_FORBIDDEN,
}


@pytest.fixture
async def as_handbook(
    handbook_app_url: str, ticketing_data: object, crm_data: object, handbook_data: object
) -> AsyncIterator[AsyncConnection]:
    # All three schemas must exist; see as_crm.
    connection = await AsyncConnection.connect(handbook_app_url, autocommit=True)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.parametrize("relation", HANDBOOK_READABLE)
async def test_the_handbook_role_reads_the_two_views_and_the_migration_list(
    as_handbook: AsyncConnection, relation: str
) -> None:
    query = f"SELECT count(*) FROM handbook.{relation}"  # noqa: S608 - from the list above
    cursor = await as_handbook.execute(query.encode())

    assert await cursor.fetchone() is not None


@pytest.mark.parametrize("statement", HANDBOOK_FORBIDDEN.values(), ids=HANDBOOK_FORBIDDEN.keys())
async def test_the_handbook_role_cannot_touch_a_base_table_or_write_anything(
    as_handbook: AsyncConnection, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_handbook.execute(statement.encode())


async def test_the_chunk_view_cannot_be_written_to(as_handbook: AsyncConnection) -> None:
    # A view over a join is not writable at all, so the refusal is not a privilege error.
    for statement in (
        b"UPDATE handbook.searchable_chunks SET text = 'rewritten'",
        b"DELETE FROM handbook.searchable_chunks",
    ):
        with pytest.raises(
            (
                psycopg.errors.InsufficientPrivilege,
                psycopg.errors.FeatureNotSupported,
                psycopg.errors.ObjectNotInPrerequisiteState,
            )
        ):
            await as_handbook.execute(statement)


async def test_the_handbook_views_hold_no_restricted_row(as_handbook: AsyncConnection) -> None:
    cursor = await as_handbook.execute(
        "SELECT (SELECT count(*) FROM handbook.published_documents),"
        " (SELECT count(*) FROM handbook.published_documents WHERE classification = 'restricted'),"
        " (SELECT count(*) FROM handbook.searchable_chunks WHERE classification = 'restricted'),"
        " (SELECT count(*) FROM handbook.published_documents"
        "  WHERE id IN ('DOC-023', 'DOC-024', 'DOC-030'))"
    )

    assert await cursor.fetchone() == (27, 0, 0, 0)


async def test_the_handbook_role_has_nothing_in_the_public_schema(
    as_handbook: AsyncConnection,
) -> None:
    cursor = await as_handbook.execute(
        "SELECT has_schema_privilege('public', 'USAGE'), has_schema_privilege('public', 'CREATE'),"
        " has_database_privilege(current_database(), 'TEMPORARY'),"
        " has_schema_privilege('handbook', 'CREATE')"
    )

    assert await cursor.fetchone() == (False, False, False, False)


# --- no other role can see the CRM or the handbook ----------------------------------------------

CRM_AND_HANDBOOK_READS = {
    "crm accounts": "SELECT count(*) FROM crm.accounts",
    "crm contacts": "SELECT count(*) FROM crm.contacts",
    "crm deals": "SELECT count(*) FROM crm.deals",
    "crm notes": "SELECT count(*) FROM crm.activity_notes",
    "crm migrations": "SELECT count(*) FROM crm.schema_migrations",
    "handbook documents view": "SELECT count(*) FROM handbook.published_documents",
    "handbook chunks view": "SELECT count(*) FROM handbook.searchable_chunks",
    "handbook documents table": "SELECT count(*) FROM handbook.documents",
    "handbook chunks table": "SELECT count(*) FROM handbook.chunks",
    "handbook migrations": "SELECT count(*) FROM handbook.schema_migrations",
}


@pytest.mark.parametrize(
    "statement", CRM_AND_HANDBOOK_READS.values(), ids=CRM_AND_HANDBOOK_READS.keys()
)
async def test_the_gateway_role_cannot_read_the_crm_or_the_handbook(
    as_gateway: AsyncConnection, crm_data: object, handbook_data: object, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_gateway.execute(statement.encode())


@pytest.mark.parametrize(
    "statement", CRM_AND_HANDBOOK_READS.values(), ids=CRM_AND_HANDBOOK_READS.keys()
)
async def test_the_ticketing_role_cannot_read_the_crm_or_the_handbook(
    as_ticketing: AsyncConnection, crm_data: object, handbook_data: object, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await as_ticketing.execute(statement.encode())


async def test_the_crm_and_handbook_roles_cannot_read_each_others_schema(
    as_crm: AsyncConnection, as_handbook: AsyncConnection
) -> None:
    for connection, query in (
        (as_crm, b"SELECT count(*) FROM handbook.schema_migrations"),
        (as_handbook, b"SELECT count(*) FROM crm.schema_migrations"),
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await connection.execute(query)


async def test_only_the_named_roles_may_connect_to_the_database(
    as_gateway: AsyncConnection,
) -> None:
    cursor = await as_gateway.execute(
        "SELECT has_database_privilege('crm_app', current_database(), 'CONNECT'),"
        " has_database_privilege('handbook_app', current_database(), 'CONNECT'),"
        " has_database_privilege('crm_app', current_database(), 'TEMPORARY'),"
        " has_database_privilege('handbook_app', current_database(), 'TEMPORARY'),"
        " has_schema_privilege('crm_app', 'public', 'USAGE'),"
        " has_schema_privilege('handbook_app', 'public', 'USAGE')"
    )

    assert await cursor.fetchone() == (True, True, False, False, False, False)
