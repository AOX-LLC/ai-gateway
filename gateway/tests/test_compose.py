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
]


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_backend_network_is_internal_and_the_edge_network_is_not(
    compose: dict[str, Any],
) -> None:
    networks = compose["networks"]

    assert networks["backend"] == {"internal": True}
    assert not (networks["edge"] or {}).get("internal")


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
