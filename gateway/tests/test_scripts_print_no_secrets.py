"""No script prints a value from the environment file.

The scripts run with stand-ins for `docker`, `uv` and `curl` (they print nothing of their own),
in a directory whose `.env` is a freshly generated one, and the test looks for every one of its
values in what the script printed."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
MIN_SECRET_LENGTH = 8
"""Shorter values (a port, a role name) are not secrets and would match by chance."""
HEALTH = {
    "telemetry": {"status": "ok", "dropped_total": 0, "rejected_total": 0},
    "audit": {"status": "ok", "dropped_total": 0, "rejected_total": 0},
}
DEMO_TOKENS = {
    "harborline-support-bot": "gw_lookupsupport_secretsupportsecretsupport",
    "harborline-ops-bot": "gw_lookupops_secretopssecretopssecretops",
}


def _values(env_file: Path) -> list[str]:
    values = []
    for line in env_file.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.startswith("#") and len(value) >= MIN_SECRET_LENGTH:
            values.append(value)
    return values


def _stand_in(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    shutil.copytree(SCRIPTS, tmp_path / "scripts")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    subprocess.run(
        [sys.executable, "scripts/init_env.py"], cwd=tmp_path, check=True, capture_output=True
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stand_in(
        bin_dir / "docker",
        'if [[ "$*" == *seed-demo* ]]; then echo \'' + json.dumps(DEMO_TOKENS) + "'; fi\n",
    )
    _stand_in(bin_dir / "uv", "exit 0\n")
    _stand_in(bin_dir / "curl", "echo '" + json.dumps(HEALTH) + "'\n")
    return tmp_path


def _run(workdir: Path, *command: str, github_actions: bool) -> str:
    env = {
        "PATH": f"{workdir / 'bin'}:{os.environ['PATH']}",
        "HOME": str(workdir),
        "RUNNER_TEMP": str(workdir),
    }
    if github_actions:
        env["GITHUB_ACTIONS"] = "true"
    done = subprocess.run(
        command, cwd=workdir, env=env, capture_output=True, text=True, check=True, timeout=60
    )
    return done.stdout + done.stderr


def test_the_scenario_script_prints_no_value_from_the_environment_file(workdir: Path) -> None:
    secrets = [*_values(workdir / ".env"), *DEMO_TOKENS.values()]

    output = _run(workdir, "scripts/run_harborline_check.sh", github_actions=False)

    assert [value for value in secrets if value in output] == []
    assert "add-mask" not in output


def test_the_check_would_notice_a_secret_in_the_output(workdir: Path) -> None:
    """Under GitHub Actions the masking commands are printed on purpose, and GitHub hides what
    they name. Run there, the same script's output holds the values: the check above can fail."""
    secrets = _values(workdir / ".env")

    output = _run(workdir, "scripts/run_harborline_check.sh", github_actions=True)

    assert any(value in output for value in secrets)
    assert "::add-mask::" in output


def test_the_environment_script_prints_names_and_never_values(workdir: Path) -> None:
    (workdir / ".env").unlink()

    output = _run(workdir, sys.executable, "scripts/init_env.py", github_actions=False)
    again = _run(workdir, sys.executable, "scripts/init_env.py", github_actions=False)

    secrets = _values(workdir / ".env")
    assert secrets
    assert [value for value in secrets if value in output + again] == []


def test_masking_is_silent_outside_github_actions(workdir: Path) -> None:
    command = ("bash", "-c", '. scripts/mask.sh; mask "a-secret-value"')

    assert _run(workdir, *command, github_actions=False) == ""
    assert _run(workdir, *command, github_actions=True) == "::add-mask::a-secret-value\n"
