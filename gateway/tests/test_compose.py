"""The Compose file's network layout: what can reach the host and what can reach the outside."""

import math
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE_FILE = Path(__file__).resolve().parents[2] / "compose.yaml"

# Services that must be unable to reach the Internet: the three MCP servers, their setup
# one-shot, the migration and admin one-shots, the in-network scenario runner and the test
# upstream. None of them needs anything outside the stack.
INTERNAL_ONLY = [
    "ticketing",
    "crm",
    "handbook",
    "servers-setup",
    "migrate",
    "admin",
    "direct-check",
    "echo",
    "telemetry-setup",
    "telemetry-purge",
    "policy-setup",
]


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_backend_network_is_internal_and_gives_the_host_no_address_on_it(
    compose: dict[str, Any],
) -> None:
    """Internal blocks the way out to the Internet; without the bridge address, a container
    also cannot reach the host's own services (SSH, anything bound to 0.0.0.0)."""
    networks = compose["networks"]

    assert networks["backend"]["internal"] is True
    assert networks["backend"]["driver_opts"] == {"com.docker.network.bridge.inhibit_ipv4": "true"}
    assert not (networks["edge"] or {}).get("internal")


def test_only_the_gateway_and_postgres_are_on_the_edge_network(compose: dict[str, Any]) -> None:
    """The edge network has a route out, so a new service must not join it unnoticed."""
    on_edge = {
        name for name, svc in compose["services"].items() if "edge" in svc.get("networks", [])
    }

    assert on_edge == {"gateway", "postgres"}


@pytest.mark.parametrize("service", INTERNAL_ONLY)
def test_a_service_with_no_business_outside_sits_only_on_the_internal_network(
    compose: dict[str, Any], service: str
) -> None:
    definition = compose["services"][service]

    assert definition["networks"] == ["backend"]
    assert "ports" not in definition, "a port cannot be published from an internal network"


def test_the_gateway_and_postgres_bridge_the_two_networks(compose: dict[str, Any]) -> None:
    assert compose["services"]["gateway"]["networks"] == ["edge", "backend"]
    # Postgres is also on the dashboard's network, which is why the dashboard can reach it.
    assert compose["services"]["postgres"]["networks"] == ["edge", "backend", "dashboard"]


DASHBOARD_DIRECTORY = COMPOSE_FILE.parent / "dashboard"


def test_the_dashboard_is_on_a_network_with_postgres_only_and_cannot_reach_the_gateway(
    compose: dict[str, Any],
) -> None:
    """It reads the database and nothing else, so it shares no network with the gateway or the
    servers. The network is not internal, since a published port cannot come from one."""
    services = compose["services"]

    assert services["dashboard"]["networks"] == ["dashboard"]
    on_dashboard = {
        name for name, svc in services.items() if "dashboard" in svc.get("networks", [])
    }
    assert on_dashboard == {"dashboard", "postgres"}
    assert not (compose["networks"]["dashboard"] or {}).get("internal")
    assert {"edge", "backend"}.isdisjoint(services["dashboard"]["networks"])


def test_the_dashboard_publishes_4400_on_loopback_and_runs_hardened(
    compose: dict[str, Any],
) -> None:
    dashboard = compose["services"]["dashboard"]

    assert dashboard["ports"] == ["127.0.0.1:4400:4400"]
    assert dashboard["read_only"] is True
    assert dashboard["cap_drop"] == ["ALL"]
    assert dashboard["security_opt"] == ["no-new-privileges:true"]
    assert dashboard["restart"] == "unless-stopped"
    assert dashboard["mem_limit"] == "144m", "1.5 x the 94 MiB measured peak (Phase 5c-3)"


def test_the_long_running_services_are_limited_to_one_and_a_half_times_their_measured_peak(
    compose: dict[str, Any],
) -> None:
    """Peaks from scripts/measure_memory_at_maxima.py (Phase 4d), limits in steps of 16 MiB. The
    gateway had sat at its old limit of 176 MiB."""
    peaks_mib = {
        "gateway": 175.1,
        "handbook": 138.1,
        "ticketing": 83.1,
        "crm": 83.6,
        "approvals-purge": 72.3,
        "telemetry-purge": 29.1,
    }
    floor = 64

    for service, peak in peaks_mib.items():
        wanted = max(math.ceil(peak * 1.5 / 16) * 16, floor)
        assert compose["services"][service]["mem_limit"] == f"{wanted}m", service
    assert compose["services"]["postgres"]["mem_limit"] == "256m", "the floor: peak 152 MiB"


def test_next_telemetry_is_disabled_in_the_dashboard_service_and_image(
    compose: dict[str, Any],
) -> None:
    assert compose["services"]["dashboard"]["environment"]["NEXT_TELEMETRY_DISABLED"] == "1"
    dockerfile = (DASHBOARD_DIRECTORY / "Dockerfile").read_text(encoding="utf-8")
    stages = dockerfile.split("\nFROM ")[1:]
    assert len(stages) == 3
    for stage in stages:
        assert "NEXT_TELEMETRY_DISABLED=1" in stage, stage.splitlines()[0]


def test_the_dashboard_holds_the_telemetry_readers_credential_and_no_other_secret(
    compose: dict[str, Any],
) -> None:
    environment = compose["services"]["dashboard"]["environment"]

    assert set(environment) == {
        "NEXT_TELEMETRY_DISABLED",
        "NODE_OPTIONS",
        "DASHBOARD_DATABASE_URL",
        "DASHBOARD_SESSION_SECRET",
        "DASHBOARD_ADMIN_PASSWORD_HASH",
        "DASHBOARD_SAMPLE_DATA",
        "DASHBOARD_ALLOWED_HOSTS",
    }
    assert environment["DASHBOARD_DATABASE_URL"].startswith("postgresql://telemetry_reader:")
    assert "POSTGRES_PASSWORD" not in str(environment)


def test_every_service_names_its_networks(compose: dict[str, Any]) -> None:
    """A service with no `networks` joins the default network, which would be a way out."""
    unnamed = [name for name, svc in compose["services"].items() if "networks" not in svc]

    assert unnamed == []


def test_every_published_port_is_bound_to_loopback(compose: dict[str, Any]) -> None:
    published = [
        (name, port) for name, svc in compose["services"].items() for port in svc.get("ports", [])
    ]

    assert published, "the gateway and Postgres publish ports"
    assert [(n, p) for n, p in published if not str(p).startswith("127.0.0.1:")] == []


MEMORY_BUDGET_MIB = 3072
"""All services together may be limited to this much. The machine this runs on is shared and
has 7 GB; the whole stack, Postgres included, has to leave room for the other work."""


def _mib(limit: str) -> int:
    units = {"m": 1, "g": 1024}
    return int(limit[:-1]) * units[limit[-1]]


def test_every_service_has_a_memory_limit(compose: dict[str, Any]) -> None:
    unlimited = [name for name, svc in compose["services"].items() if "mem_limit" not in svc]

    assert unlimited == []


def test_the_memory_limits_together_fit_a_shared_machine(compose: dict[str, Any]) -> None:
    total = sum(_mib(str(svc["mem_limit"])) for svc in compose["services"].values())

    assert total <= MEMORY_BUDGET_MIB


def test_long_running_services_restart_and_finished_one_shots_do_not(
    compose: dict[str, Any],
) -> None:
    """The purge that enforces the retention limits must not stay down after a reboot or an
    out-of-memory kill; a setup job that has finished must not start over."""
    services = compose["services"]

    for name in (
        "gateway",
        "postgres",
        "crm",
        "ticketing",
        "handbook",
        "telemetry-purge",
        "dashboard",
    ):
        assert services[name]["restart"] == "unless-stopped", name
    for name in ("migrate", "telemetry-setup", "policy-setup", "servers-setup", "lab-approver"):
        assert services[name]["restart"] == "no", name


def test_the_postgres_healthcheck_asks_over_tcp_with_the_right_user_and_database(
    compose: dict[str, Any],
) -> None:
    """The database image runs a temporary server on a Unix socket while it initialises a new
    volume. A check that uses the socket can report healthy then, and a setup service that starts
    in that moment fails with "the database system is starting up". Only the real server listens
    on TCP."""
    postgres = compose["services"]["postgres"]

    check = postgres["healthcheck"]["test"]

    assert check[0] == "CMD-SHELL"
    assert check[1].split()[:3] == ["pg_isready", "-h", "127.0.0.1"]
    assert "-U $$POSTGRES_USER" in check[1]
    assert "-d $$POSTGRES_DB" in check[1]
    assert postgres["environment"]["POSTGRES_USER"]
    assert postgres["environment"]["POSTGRES_DB"]


def test_the_ci_database_service_checks_over_tcp_too() -> None:
    workflow = yaml.safe_load((COMPOSE_FILE.parent / ".github/workflows/ci.yml").read_text())
    postgres = workflow["jobs"]["test"]["services"]["postgres"]

    options = " ".join(postgres["options"].split())

    assert (
        '--health-cmd "pg_isready -h 127.0.0.1 -U ai_gateway_owner -d ai_gateway_test"' in options
    )
    assert postgres["env"]["POSTGRES_USER"] == "ai_gateway_owner"
    assert postgres["env"]["POSTGRES_DB"] == "ai_gateway_test"
