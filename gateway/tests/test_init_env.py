"""scripts/init_env.py: strong secrets in place of the placeholders, URLs kept consistent."""

import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "init_env.py"
EXAMPLE = (
    "# comment with change-me inside is left alone\n"
    "OWNER_PASSWORD=change-me-owner\n"
    "OWNER_URL=postgresql://owner:change-me-owner@db:5432/app\n"
    "APP_PASSWORD=change-me-app\n"
    "APP_URL=postgresql://app:change-me-app@db:5432/app\n"
    "SERVICE_TOKEN=change-me-service-token\n"
    "PORT=4401\n"
    "TOKEN=\n"
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def _values(path: Path) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    return {key: value for key, value in pairs if not key.startswith("#")}


@pytest.fixture
def example(tmp_path: Path) -> Path:
    path = tmp_path / ".env.example"
    path.write_text(EXAMPLE)
    return path


def test_every_placeholder_becomes_a_fresh_strong_secret(example: Path, tmp_path: Path) -> None:
    output = tmp_path / ".env"

    result = _run("--example", str(example), "--output", str(output))

    values = _values(output)
    assert result.returncode == 0
    assert "change-me" not in "".join(values.values())
    secret_keys = ["OWNER_PASSWORD", "APP_PASSWORD", "SERVICE_TOKEN"]
    secrets_ = [values[key] for key in secret_keys]
    assert all(len(secret) >= 32 for secret in secrets_)
    assert len(set(secrets_)) == 3
    assert values["PORT"] == "4401"
    assert values["TOKEN"] == ""
    for secret in secrets_:
        assert secret not in result.stdout + result.stderr


def test_urls_carry_the_new_passwords(example: Path, tmp_path: Path) -> None:
    output = tmp_path / ".env"
    _run("--example", str(example), "--output", str(output))

    values = _values(output)

    assert values["OWNER_URL"] == f"postgresql://owner:{values['OWNER_PASSWORD']}@db:5432/app"
    assert values["APP_URL"] == f"postgresql://app:{values['APP_PASSWORD']}@db:5432/app"
    assert re.fullmatch(r"[A-Za-z0-9_-]+", values["OWNER_PASSWORD"])


def test_each_run_makes_different_secrets(example: Path, tmp_path: Path) -> None:
    _run("--example", str(example), "--output", str(tmp_path / "a"))
    _run("--example", str(example), "--output", str(tmp_path / "b"))

    assert _values(tmp_path / "a")["SERVICE_TOKEN"] != _values(tmp_path / "b")["SERVICE_TOKEN"]


def test_an_existing_env_is_not_overwritten_without_force(example: Path, tmp_path: Path) -> None:
    output = tmp_path / ".env"
    output.write_text("KEEP=me\n")

    refused = _run("--example", str(example), "--output", str(output))
    forced = _run("--example", str(example), "--output", str(output), "--force")

    assert refused.returncode != 0
    assert "--force" in refused.stderr
    assert forced.returncode == 0
    assert "KEEP" not in output.read_text()


def test_the_file_is_readable_by_its_owner_only(example: Path, tmp_path: Path) -> None:
    output = tmp_path / ".env"
    _run("--example", str(example), "--output", str(output))

    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_the_real_example_has_no_placeholder_left_after_init(tmp_path: Path) -> None:
    output = tmp_path / ".env"

    _run("--example", str(REPO_ROOT / ".env.example"), "--output", str(output))

    assert "change-me" not in output.read_text()
