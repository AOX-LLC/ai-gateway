"""The Compose file's network layout: what can reach the host and what can reach the outside."""

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
    for service in ("gateway", "postgres"):
        assert compose["services"][service]["networks"] == ["edge", "backend"]


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

    for name in ("gateway", "postgres", "crm", "ticketing", "handbook", "telemetry-purge"):
        assert services[name]["restart"] == "unless-stopped", name
    for name in ("migrate", "telemetry-setup", "policy-setup", "servers-setup", "lab-approver"):
        assert services[name]["restart"] == "no", name
