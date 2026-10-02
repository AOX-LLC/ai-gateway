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


def test_force_regenerates_an_existing_env(example: Path, tmp_path: Path) -> None:
    output = tmp_path / ".env"
    output.write_text("KEEP=me\n")

    forced = _run("--example", str(example), "--output", str(output), "--force")

    assert forced.returncode == 0
    assert "KEEP" not in output.read_text()


def _existing_env(tmp_path: Path, text: str) -> Path:
    output = tmp_path / ".env"
    output.write_text(text)
    output.chmod(0o600)
    return output


def test_an_existing_env_only_gains_the_keys_it_is_missing(example: Path, tmp_path: Path) -> None:
    before = "OWNER_PASSWORD=my-own-owner-password\nPORT=9999\n# my note\nTOKEN=kept\n"
    output = _existing_env(tmp_path, before)

    result = _run("--example", str(example), "--output", str(output))

    after = output.read_text()
    values = _values(output)
    assert result.returncode == 0
    assert after.startswith(before)
    assert set(values) == {
        "OWNER_PASSWORD", "OWNER_URL", "APP_PASSWORD", "APP_URL", "SERVICE_TOKEN", "PORT", "TOKEN",
    }  # fmt: skip
    assert "change-me" not in after
    assert len(values["APP_PASSWORD"]) >= 32
    assert values["SERVICE_TOKEN"] != values["APP_PASSWORD"]
    for secret in (values["APP_PASSWORD"], values["SERVICE_TOKEN"]):
        assert secret not in result.stdout + result.stderr


def test_new_urls_carry_the_passwords_of_the_resulting_file(example: Path, tmp_path: Path) -> None:
    # The owner password already exists; its URL is new and must carry that very password.
    output = _existing_env(tmp_path, "OWNER_PASSWORD=my-own-owner-password\n")

    _run("--example", str(example), "--output", str(output))

    values = _values(output)
    assert values["OWNER_PASSWORD"] == "my-own-owner-password"  # noqa: S105 - fixture value
    assert values["OWNER_URL"] == "postgresql://owner:my-own-owner-password@db:5432/app"
    assert values["APP_URL"] == f"postgresql://app:{values['APP_PASSWORD']}@db:5432/app"


def test_a_second_run_changes_nothing(example: Path, tmp_path: Path) -> None:
    output = _existing_env(tmp_path, "PORT=1\n")
    _run("--example", str(example), "--output", str(output))
    first = output.read_bytes()
    first_stat = output.stat()

    second = _run("--example", str(example), "--output", str(output))

    assert second.returncode == 0
    assert "nothing to add" in second.stdout
    assert output.read_bytes() == first
    assert output.stat().st_mtime_ns == first_stat.st_mtime_ns


def test_the_added_keys_keep_the_comments_above_them_and_the_mode(
    tmp_path: Path,
) -> None:
    example = tmp_path / ".env.example"
    example.write_text("A=1\n# about B\n# more about B\nB=change-me-b\nC=3\n")
    output = _existing_env(tmp_path, "A=1")  # no trailing newline

    _run("--example", str(example), "--output", str(output))

    lines = output.read_text().splitlines()
    assert lines[0] == "A=1"
    assert lines[1:3] == ["# about B", "# more about B"]
    assert lines[3].startswith("B=")
    assert len(lines[3]) > 40
    assert lines[4] == "C=3"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_the_file_is_readable_by_its_owner_only(example: Path, tmp_path: Path) -> None:
    output = tmp_path / ".env"
    _run("--example", str(example), "--output", str(output))

    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_the_real_example_has_no_placeholder_left_after_init(tmp_path: Path) -> None:
    output = tmp_path / ".env"

    _run("--example", str(REPO_ROOT / ".env.example"), "--output", str(output))

    assert "change-me" not in output.read_text()
