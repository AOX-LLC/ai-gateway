"""One version everywhere: every package, the dashboard, and what the gateway reports of itself."""

import json
import tomllib
from importlib.metadata import version
from pathlib import Path

from ai_gateway.proxy.server import GatewayServer
from ai_gateway.proxy.upstream_client import _GATEWAY_IDENTITY

ROOT = Path(__file__).resolve().parents[2]


def _declared() -> dict[str, str]:
    found = {}
    for path in [
        ROOT / "gateway/pyproject.toml",
        *sorted((ROOT / "servers").glob("*/pyproject.toml")),
    ]:
        found[str(path.relative_to(ROOT))] = tomllib.loads(path.read_text())["project"]["version"]
    package = json.loads((ROOT / "dashboard/package.json").read_text())
    lock = json.loads((ROOT / "dashboard/package-lock.json").read_text())
    found["dashboard/package.json"] = package["version"]
    found["dashboard/package-lock.json"] = lock["version"]
    found["dashboard/package-lock.json (root package)"] = lock["packages"][""]["version"]
    return found


def test_every_package_and_the_dashboard_declare_one_version() -> None:
    declared = _declared()

    assert len(declared) >= 10
    assert set(declared.values()) == {declared["gateway/pyproject.toml"]}, declared


def test_the_lock_file_agrees_with_the_packages() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {
        p["name"]: p["version"] for p in lock["package"] if p.get("source", {}).get("editable")
    }
    wanted = _declared()["gateway/pyproject.toml"]

    assert locked["ai-gateway"] == wanted
    assert set(locked.values()) == {wanted}, locked


def test_what_the_gateway_reports_of_itself_is_the_declared_version() -> None:
    declared = _declared()["gateway/pyproject.toml"]
    served = GatewayServer(object(), object(), object()).build()  # type: ignore[arg-type]

    assert version("ai-gateway") == declared
    assert _GATEWAY_IDENTITY.version == declared
    assert served.version == declared
