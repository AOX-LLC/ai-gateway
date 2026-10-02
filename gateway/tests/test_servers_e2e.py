"""All three Harborline servers behind the gateway, over real HTTP, as the demo runs them:
the two demo clients with their exact scopes, the service credentials and /healthz."""

from collections.abc import Awaitable, Callable, Iterator
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS
from pydantic import SecretStr
from starlette.applications import Starlette

from ai_gateway.admin.cli import DEMO_SCOPES_OPS, DEMO_SCOPES_SUPPORT
from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.registry.repo import AdminRegistry
from ai_gateway.registry.tool_policies import load_tool_policies
from crm_server import MIGRATIONS_PACKAGE as CRM_MIGRATIONS_PACKAGE
from crm_server.seed import Dataset as CrmDataset
from crm_server.server import build_app as build_crm_app
from crm_server.settings import CrmSettings
from handbook_server import MIGRATIONS_PACKAGE as HANDBOOK_MIGRATIONS_PACKAGE
from handbook_server.embedding import ModelNotFoundError
from handbook_server.server import build_app as build_handbook_app
from handbook_server.settings import HandbookSettings
from harborline_setup.handbook_seed import Dataset as HandbookDataset
from mcp_common.credentials import MissingCredentialError, WeakCredentialError
from mcp_common.migrate import load_migrations
from mcp_common.notice import FICTIONAL_NOTICE
from mcp_common.schema_contract import check_input_schema
from tests.helpers import RunningGateway, connect, run_gateway, serve_in_thread
from ticketing_server import MIGRATIONS_PACKAGE as TICKETING_MIGRATIONS_PACKAGE
from ticketing_server.seed import Dataset as TicketingDataset
from ticketing_server.server import build_app as build_ticketing_app
from ticketing_server.settings import TicketingSettings

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MakeClient = Callable[..., Awaitable[tuple[UUID, IssuedToken]]]

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKENS = {  # fixture values, one per server
    "TICKETING_SERVICE_TOKEN": "ticketing-service-token-for-tests-only",
    "CRM_SERVICE_TOKEN": "crm-service-token-for-the-tests-only-0123",
    "HANDBOOK_SERVICE_TOKEN": "handbook-service-token-for-tests-only-45",
}
READ_ONLY = {
    "tickets__get_ticket",
    "tickets__list_tickets",
    "crm__search_accounts",
    "crm__get_account",
    "crm__list_deals",
    "handbook__search",
    "handbook__get_document",
}
HEALTH_KEYS = {
    "status", "commit", "commit_source", "branch", "version", "schema_version", "uptime_s",
}  # fmt: skip


@pytest.fixture
def servers(
    ticketing_app_url: str,
    crm_app_url: str,
    handbook_app_url: str,
    model_path: Path,
    ticketing_data: TicketingDataset,
    crm_data: CrmDataset,
    handbook_data: HandbookDataset,
) -> Iterator[dict[str, str]]:
    """Base URLs of the three running servers, keyed by namespace."""
    apps: dict[str, Starlette] = {
        "tickets": build_ticketing_app(
            TicketingSettings(
                database_url=SecretStr(ticketing_app_url),
                service_token=SecretStr(TOKENS["TICKETING_SERVICE_TOKEN"]),
                allowed_hosts=["127.0.0.1:*"],
            )
        ),
        "crm": build_crm_app(
            CrmSettings(
                database_url=SecretStr(crm_app_url),
                service_token=SecretStr(TOKENS["CRM_SERVICE_TOKEN"]),
                allowed_hosts=["127.0.0.1:*"],
            )
        ),
        "handbook": build_handbook_app(
            HandbookSettings(
                database_url=SecretStr(handbook_app_url),
                service_token=SecretStr(TOKENS["HANDBOOK_SERVICE_TOKEN"]),
                model_path=model_path,
                allowed_hosts=["127.0.0.1:*"],
            )
        ),
    }
    with (
        serve_in_thread(apps["tickets"]) as tickets,
        serve_in_thread(apps["crm"]) as crm,
        serve_in_thread(apps["handbook"]) as handbook,
    ):
        yield {"tickets": tickets, "crm": crm, "handbook": handbook}


CREDENTIAL_OF = {
    "tickets": "TICKETING_SERVICE_TOKEN",
    "crm": "CRM_SERVICE_TOKEN",
    "handbook": "HANDBOOK_SERVICE_TOKEN",
}


@pytest.fixture
async def tokens(
    admin_registry: AdminRegistry,
    make_client: MakeClient,
    servers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, IssuedToken]:
    for namespace, base_url in servers.items():
        env = CREDENTIAL_OF[namespace]
        monkeypatch.setenv(env, TOKENS[env])
        await admin_registry.upsert_upstream(namespace, f"{base_url}/mcp", 2000, 5000, env)
    for policy in load_tool_policies(REPO_ROOT / "config" / "tool_policies.toml"):
        await admin_registry.upsert_tool_policy(
            policy.namespace, policy.tool, policy.effect, policy.notes
        )
    _, support = await make_client("harborline-support-bot", DEMO_SCOPES_SUPPORT)
    _, ops = await make_client("harborline-ops-bot", DEMO_SCOPES_OPS)
    return {"support": support, "ops": ops}


@pytest.fixture
def gateway(
    test_database_url: str, tokens: dict[str, IssuedToken], tmp_path: Path
) -> Iterator[RunningGateway]:
    with run_gateway(test_database_url, tmp_path) as running:
        yield running


# --- what each client sees through the gateway ------------------------------------------------


async def test_the_ops_client_sees_all_eleven_tools_and_each_schema_meets_the_contract(
    gateway: RunningGateway, tokens: dict[str, IssuedToken]
) -> None:
    async with connect(gateway.url, tokens["ops"].plaintext) as client:
        tools = (await client.list_tools()).tools

    assert sorted(tool.name for tool in tools) == sorted(DEMO_SCOPES_OPS)
    assert len(tools) == 11
    for tool in tools:
        assert check_input_schema(tool.input_schema) == [], tool.name
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is (tool.name in READ_ONLY), tool.name


async def test_the_support_client_sees_exactly_nine_tools_and_cannot_call_the_others(
    gateway: RunningGateway, tokens: dict[str, IssuedToken]
) -> None:
    async with connect(gateway.url, tokens["support"].plaintext) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        refused = []
        for name, arguments in (
            ("tickets__change_status", {"ticket_id": "TKT-000001", "status": "closed"}),
            ("tickets__assign", {"ticket_id": "TKT-000001", "assignee": "a.b"}),
        ):
            with pytest.raises(MCPError) as error:
                await client.call_tool(name, arguments)
            refused.append(error.value.code)

    assert listed == set(DEMO_SCOPES_SUPPORT)
    assert len(listed) == 9
    assert refused == [INVALID_PARAMS, INVALID_PARAMS]


async def test_the_clients_reach_the_crm_and_the_handbook_through_the_gateway(
    gateway: RunningGateway, tokens: dict[str, IssuedToken]
) -> None:
    async with connect(gateway.url, tokens["support"].plaintext) as client:
        accounts = await client.call_tool("crm__search_accounts", {"query": "marina"})
        account = await client.call_tool("crm__get_account", {"account_id": "ACC-00001"})
        deals = await client.call_tool("crm__list_deals", {"stage": "won"})
        found = await client.call_tool("handbook__search", {"query": "vacation days"})
        document = await client.call_tool("handbook__get_document", {"document_id": "DOC-001"})
        restricted = await client.call_tool("handbook__get_document", {"document_id": "DOC-023"})
        missing = await client.call_tool("handbook__get_document", {"document_id": "DOC-998"})

    for result in (accounts, account, deals, found, document):
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["notice"] == FICTIONAL_NOTICE
    assert restricted.is_error is True
    assert missing.is_error is True
    assert [b.model_dump() for b in restricted.content if hasattr(b, "text")] == [
        {**b.model_dump(), "text": b.text.replace("DOC-998", "DOC-023")}  # type: ignore[union-attr]
        for b in missing.content
    ]


@pytest.mark.parametrize("namespace", ["tickets", "crm", "handbook"])
async def test_each_servers_own_tool_schemas_meet_the_contract(
    servers: dict[str, str], namespace: str
) -> None:
    async with connect(f"{servers[namespace]}/mcp", TOKENS[CREDENTIAL_OF[namespace]]) as client:
        tools = (await client.list_tools()).tools

    assert tools
    for tool in tools:
        assert check_input_schema(tool.input_schema) == [], tool.name


# --- credentials and health -----------------------------------------------------------------------

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "raw-test", "version": "0"},
    },
}


@pytest.mark.parametrize("namespace", ["tickets", "crm", "handbook"])
async def test_every_server_refuses_calls_without_its_own_credential(
    servers: dict[str, str], namespace: str
) -> None:
    url = f"{servers[namespace]}/mcp"
    headers = {"Accept": "application/json, text/event-stream"}
    other = next(env for ns, env in CREDENTIAL_OF.items() if ns != namespace)
    async with httpx2.AsyncClient() as http:
        missing = await http.post(url, json=_INITIALIZE, headers=headers)
        wrong = await http.post(
            url, json=_INITIALIZE, headers={**headers, "Authorization": "Bearer nope"}
        )
        another_servers = await http.post(
            url, json=_INITIALIZE, headers={**headers, "Authorization": f"Bearer {TOKENS[other]}"}
        )
        right = await http.post(
            url,
            json=_INITIALIZE,
            headers={**headers, "Authorization": f"Bearer {TOKENS[CREDENTIAL_OF[namespace]]}"},
        )

    assert [r.status_code for r in (missing, wrong, another_servers, right)] == [
        401, 401, 401, 200,
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("namespace", "package", "migrations"),
    [
        ("tickets", "ticketing-server", TICKETING_MIGRATIONS_PACKAGE),
        ("crm", "crm-server", CRM_MIGRATIONS_PACKAGE),
        ("handbook", "handbook-server", HANDBOOK_MIGRATIONS_PACKAGE),
    ],
)
async def test_healthz_reports_the_servers_identity_in_the_house_format(
    servers: dict[str, str], namespace: str, package: str, migrations: str
) -> None:
    async with httpx2.AsyncClient() as http:
        response = await http.get(f"{servers[namespace]}/healthz")  # no credential needed

    health = response.json()
    newest = max(m.version for m in load_migrations(migrations))
    assert response.status_code == 200
    assert set(health) == HEALTH_KEYS
    assert health["status"] == "ok"
    assert health["commit_source"] == "process_start"
    assert health["version"] == version(package)
    assert health["schema_version"] == f"{newest:04d}"


# --- refusing to start ----------------------------------------------------------


@pytest.mark.parametrize("token", ["", "short", "change-me-" + "x" * 40])
def test_the_crm_and_handbook_servers_refuse_to_start_without_a_real_credential(
    crm_app_url: str, handbook_app_url: str, model_path: Path, token: str
) -> None:
    crm = CrmSettings(
        database_url=SecretStr(crm_app_url), service_token=SecretStr(token), allowed_hosts=[]
    )
    handbook = HandbookSettings(
        database_url=SecretStr(handbook_app_url),
        service_token=SecretStr(token),
        model_path=model_path,
        allowed_hosts=[],
    )

    with pytest.raises((MissingCredentialError, WeakCredentialError)):
        build_crm_app(crm)
    with pytest.raises((MissingCredentialError, WeakCredentialError)):
        build_handbook_app(handbook)


def test_the_handbook_server_refuses_to_start_without_its_model(
    handbook_app_url: str, tmp_path: Path
) -> None:
    settings = HandbookSettings(
        database_url=SecretStr(handbook_app_url),
        service_token=SecretStr(TOKENS["HANDBOOK_SERVICE_TOKEN"]),
        model_path=tmp_path / "no-model-here",
        allowed_hosts=[],
    )

    with pytest.raises(ModelNotFoundError, match="fetch_model"):
        build_handbook_app(settings)
