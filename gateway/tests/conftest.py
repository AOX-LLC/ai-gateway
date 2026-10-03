"""Shared fixtures: the Postgres test database, registry helpers and the echo MCP server."""

import importlib.util
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool

from ai_gateway.auth.tokens import IssuedToken, generate_token
from ai_gateway.policy import SCHEMA as POLICY_SCHEMA
from ai_gateway.policy.setup import PolicyPasswords, setup_policy
from ai_gateway.registry import MIGRATIONS_PACKAGE
from ai_gateway.registry.repo import AdminRegistry
from ai_gateway.telemetry import SCHEMA as TELEMETRY_SCHEMA
from ai_gateway.telemetry.setup import TelemetryPasswords, setup_telemetry
from crm_server import CONNECTION_KWARGS as CRM_CONNECTION_KWARGS
from crm_server import MIGRATIONS_PACKAGE as CRM_MIGRATIONS_PACKAGE
from crm_server import ROLE as CRM_ROLE
from crm_server import SCHEMA as CRM_SCHEMA
from crm_server.seed import Dataset as CrmDataset
from crm_server.seed import build_dataset as build_crm_dataset
from crm_server.seed import insert_dataset as insert_crm_dataset
from echo_server.server import build_app
from handbook_server import CONNECTION_KWARGS as HANDBOOK_CONNECTION_KWARGS
from handbook_server import MIGRATIONS_PACKAGE as HANDBOOK_MIGRATIONS_PACKAGE
from handbook_server import ROLE as HANDBOOK_ROLE
from handbook_server import SCHEMA as HANDBOOK_SCHEMA
from handbook_server.embedding import Embedder
from harborline_setup.crm import grant_crm_access
from harborline_setup.handbook import grant_handbook_access, prepare_schema
from harborline_setup.handbook_seed import Dataset as HandbookDataset
from harborline_setup.handbook_seed import build_dataset as build_handbook_dataset
from harborline_setup.handbook_seed import insert_dataset as insert_handbook_dataset
from harborline_setup.ticketing import grant_ticketing_access
from mcp_common.migrate import apply_migrations
from mcp_common.roles import restrict_database_access
from tests.helpers import HANDBOOK_DOCUMENTS, serve_in_thread
from ticketing_server import CONNECTION_KWARGS as TICKETING_CONNECTION_KWARGS
from ticketing_server import MIGRATIONS_PACKAGE as TICKETING_MIGRATIONS_PACKAGE
from ticketing_server import ROLE as TICKETING_ROLE
from ticketing_server import SCHEMA as TICKETING_SCHEMA
from ticketing_server.seed import Dataset, build_dataset, insert_dataset

MakeClient = Callable[..., Awaitable[tuple[UUID, IssuedToken]]]


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
async def test_database_url(anyio_backend: str) -> str:
    url = os.environ.get("GATEWAY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("GATEWAY_TEST_DATABASE_URL is not set")
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute("DROP SCHEMA public CASCADE")
        await connection.execute("CREATE SCHEMA public")
        # The same model as harborline-setup: PUBLIC gets nothing, and the roles that need
        # the database and its public schema (the gateway's) are granted it by name.
        await restrict_database_access(connection, [TICKETING_ROLE, CRM_ROLE, HANDBOOK_ROLE])
    await apply_migrations(url, MIGRATIONS_PACKAGE)
    return url


@pytest.fixture
def app_database_url(test_database_url: str) -> str:
    """The test database as the gateway's own least-privilege role, gateway_app."""
    url = os.environ.get("GATEWAY_TEST_APP_DATABASE_URL")
    if not url:
        pytest.skip("GATEWAY_TEST_APP_DATABASE_URL is not set")
    return url


@pytest.fixture
async def clean_database(test_database_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(test_database_url) as connection:
        await connection.execute(
            "TRUNCATE clients, client_tokens, client_scopes, upstream_servers, tool_policies"
            " RESTART IDENTITY CASCADE"
        )


@pytest.fixture
async def db_pool(
    test_database_url: str, clean_database: None
) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(test_database_url, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def admin_registry(
    test_database_url: str, clean_database: None
) -> AsyncIterator[AdminRegistry]:
    connection = await psycopg.AsyncConnection.connect(test_database_url)
    try:
        yield AdminRegistry(connection)
    finally:
        await connection.close()


@pytest.fixture
def make_client(admin_registry: AdminRegistry) -> MakeClient:
    async def make(
        name: str, scopes: list[str], *, expires_at: datetime | None = None
    ) -> tuple[UUID, IssuedToken]:
        client_id = await admin_registry.upsert_client(name)
        await admin_registry.grant_scopes(client_id, scopes)
        token = generate_token()
        await admin_registry.insert_token(
            client_id, token.lookup_id, token.token_sha256, "test", expires_at
        )
        return client_id, token

    return make


@pytest.fixture(scope="session")
def echo_url() -> Iterator[str]:
    with serve_in_thread(build_app(["127.0.0.1:*"])) as base_url:
        yield f"{base_url}/mcp"


@pytest.fixture
def ticketing_app_url(test_database_url: str) -> str:
    """The test database as the ticketing server's own role, ticketing_app."""
    url = os.environ.get("TICKETING_TEST_APP_DATABASE_URL")
    if not url:
        pytest.skip("TICKETING_TEST_APP_DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session")
async def ticketing_schema(test_database_url: str) -> None:
    """A fresh ticketing schema, built the way harborline-setup builds it."""
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await conn.execute("DROP SCHEMA IF EXISTS ticketing CASCADE")
        await conn.execute("CREATE SCHEMA ticketing")
        await conn.execute("REVOKE ALL ON SCHEMA ticketing FROM PUBLIC")
    await apply_migrations(test_database_url, TICKETING_MIGRATIONS_PACKAGE, TICKETING_SCHEMA)
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await grant_ticketing_access(conn)


@pytest.fixture
async def ticketing_data(test_database_url: str, ticketing_schema: None) -> Dataset:
    """The deterministic seed, reloaded before every test so writes never leak across tests."""
    dataset = build_dataset()
    async with await psycopg.AsyncConnection.connect(test_database_url) as conn:
        await conn.execute("SET search_path TO ticketing")
        await conn.execute("TRUNCATE comments, tickets, staff RESTART IDENTITY CASCADE")
        await conn.execute("ALTER SEQUENCE ticket_number RESTART")
        await insert_dataset(conn, dataset)
    return dataset


@pytest.fixture
async def ticketing_pool(
    ticketing_app_url: str, ticketing_data: Dataset
) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(ticketing_app_url, kwargs=TICKETING_CONNECTION_KWARGS, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def crm_app_url(test_database_url: str) -> str:
    """The test database as the CRM server's own role, crm_app."""
    url = os.environ.get("CRM_TEST_APP_DATABASE_URL")
    if not url:
        pytest.skip("CRM_TEST_APP_DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session")
async def crm_schema(test_database_url: str) -> None:
    """A fresh crm schema, built the way harborline-setup builds it."""
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {CRM_SCHEMA} CASCADE".encode())
        await conn.execute(f"CREATE SCHEMA {CRM_SCHEMA}".encode())
        await conn.execute(f"REVOKE ALL ON SCHEMA {CRM_SCHEMA} FROM PUBLIC".encode())
    await apply_migrations(test_database_url, CRM_MIGRATIONS_PACKAGE, CRM_SCHEMA)
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await grant_crm_access(conn)


@pytest.fixture
async def crm_data(test_database_url: str, crm_schema: None) -> CrmDataset:
    """The deterministic CRM seed, reloaded before every test."""
    dataset = build_crm_dataset()
    async with await psycopg.AsyncConnection.connect(test_database_url) as conn:
        await conn.execute(f"SET search_path TO {CRM_SCHEMA}".encode())
        await conn.execute(
            "TRUNCATE activity_notes, deals, contacts, accounts RESTART IDENTITY CASCADE"
        )
        await insert_crm_dataset(conn, dataset)
    return dataset


@pytest.fixture
async def crm_pool(crm_app_url: str, crm_data: CrmDataset) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(crm_app_url, kwargs=CRM_CONNECTION_KWARGS, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


DEFAULT_MODEL_DIR = Path.home() / ".cache" / "ai-gateway" / "models" / "potion-base-8M"
_FETCH_MODEL = Path(__file__).resolve().parents[2] / "scripts" / "fetch_model.py"


@pytest.fixture(scope="session")
def model_path() -> Path:
    """The pinned embedding model: HANDBOOK_MODEL_PATH, or a cache directory under the home
    directory. Missing or damaged files are fetched, hash-verified, by scripts/fetch_model.py."""
    path = Path(os.environ.get("HANDBOOK_MODEL_PATH") or DEFAULT_MODEL_DIR)
    spec = importlib.util.spec_from_file_location("fetch_model_script", _FETCH_MODEL)
    assert spec is not None
    assert spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    script.fetch(path)
    return path


@pytest.fixture(scope="session")
def embedder(model_path: Path) -> Embedder:
    return Embedder(model_path)


@pytest.fixture(scope="session")
def handbook_dataset(embedder: Embedder) -> HandbookDataset:
    return build_handbook_dataset(embedder, HANDBOOK_DOCUMENTS)


@pytest.fixture
def handbook_app_url(test_database_url: str) -> str:
    """The test database as the handbook server's own role, handbook_app."""
    url = os.environ.get("HANDBOOK_TEST_APP_DATABASE_URL")
    if not url:
        pytest.skip("HANDBOOK_TEST_APP_DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session")
async def handbook_data(
    test_database_url: str, handbook_dataset: HandbookDataset
) -> HandbookDataset:
    """A fresh handbook schema, built and seeded the way harborline-setup builds it. The
    server only reads, so it is built once per run."""
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {HANDBOOK_SCHEMA} CASCADE".encode())
        await prepare_schema(conn)
    await apply_migrations(test_database_url, HANDBOOK_MIGRATIONS_PACKAGE, HANDBOOK_SCHEMA)
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await grant_handbook_access(conn)
    async with await psycopg.AsyncConnection.connect(test_database_url) as conn:
        await conn.execute(f"SET search_path TO {HANDBOOK_SCHEMA}".encode())
        await insert_handbook_dataset(conn, handbook_dataset)
    return handbook_dataset


@pytest.fixture
async def handbook_pool(
    handbook_app_url: str, handbook_data: HandbookDataset
) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(handbook_app_url, kwargs=HANDBOOK_CONNECTION_KWARGS, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def scratch_database(test_database_url: str) -> AsyncIterator[tuple[str, str]]:
    """An empty database and a role name that exist nowhere yet, removed afterwards."""
    suffix = uuid4().hex[:8]
    database, role = f"setup_scratch_{suffix}", f"ticketing_scratch_{suffix}"
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        yield make_conninfo(test_database_url, dbname=database), role
    finally:
        async with await psycopg.AsyncConnection.connect(
            test_database_url, autocommit=True
        ) as conn:
            await conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )
            await conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture(scope="session")
def _recording_tracer() -> InMemorySpanExporter:
    """Record every finished span in memory.

    OpenTelemetry lets the process's tracer provider be set once, and a test that starts a gateway
    with telemetry installs one first. So this uses the SDK provider if there is one, and sets its
    own if there is not: a recorder added to the provider that is current, either way."""
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture
def spans(_recording_tracer: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    """The spans finished during one test."""
    _recording_tracer.clear()
    yield _recording_tracer
    _recording_tracer.clear()


# --- telemetry ----------------------------------------------------------------------------------


def _url(variable: str) -> str:
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"{variable} is not set")
    return url


@pytest.fixture
def writer_url() -> str:
    return _url("TELEMETRY_WRITER_TEST_DATABASE_URL")


@pytest.fixture
def reader_url() -> str:
    return _url("TELEMETRY_READER_TEST_DATABASE_URL")


@pytest.fixture
def purger_url() -> str:
    return _url("TELEMETRY_PURGER_TEST_DATABASE_URL")


def password_of(url: str) -> str:
    return str(conninfo_to_dict(url)["password"])


@pytest.fixture
async def telemetry(
    test_database_url: str, writer_url: str, reader_url: str, purger_url: str
) -> None:
    """A fresh telemetry schema, set up the way `gateway-admin telemetry-setup` sets it up.
    The roles use the passwords the rest of the suite logs in with."""
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {TELEMETRY_SCHEMA} CASCADE".encode())
    await setup_telemetry(
        test_database_url,
        TelemetryPasswords(
            password_of(writer_url), password_of(reader_url), password_of(purger_url)
        ),
    )


# --- policy (the audit log and the approval queue) ------------------------------------------


@pytest.fixture
def policy_gateway_url() -> str:
    return _url("POLICY_GATEWAY_TEST_DATABASE_URL")


@pytest.fixture
def policy_approver_url() -> str:
    return _url("POLICY_APPROVER_TEST_DATABASE_URL")


@pytest.fixture
def policy_auditor_url() -> str:
    return _url("POLICY_AUDITOR_TEST_DATABASE_URL")


@pytest.fixture
async def policy(
    test_database_url: str,
    policy_gateway_url: str,
    policy_approver_url: str,
    policy_auditor_url: str,
) -> None:
    """A fresh policy schema, set up the way `gateway-admin policy-setup` sets it up. The roles use
    the passwords the rest of the suite logs in with."""
    async with await psycopg.AsyncConnection.connect(test_database_url, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {POLICY_SCHEMA} CASCADE".encode())
    await setup_policy(
        test_database_url,
        PolicyPasswords(
            password_of(policy_gateway_url),
            password_of(policy_approver_url),
            password_of(policy_auditor_url),
        ),
    )
