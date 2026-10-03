"""scripts/set_dashboard_password.py: the admin credential is written as a hash, privately, and
never printed."""

import importlib.util
import stat
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "set_dashboard_password.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("set_dashboard_password", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load()
PASSWORD = "a long enough passphrase"  # noqa: S105 - a test value


def test_the_hash_is_salted_has_no_dollar_sign_and_matches_the_dashboards_vector() -> None:
    first, second = script.hash_password(PASSWORD), script.hash_password(PASSWORD)

    assert first != second
    assert "$" not in first
    assert first.split(":")[:4] == ["scrypt", "32768", "8", "1"]
    # The same vector the dashboard's own test checks, so the two implementations cannot drift.
    fixed = script.hash_password("correct horse battery", salt=bytes(range(16)))
    assert fixed == (
        "scrypt:32768:8:1:AAECAwQFBgcICQoLDA0ODw:xy6UCICz8Vv_P6JqOkkUD4DOmhxN0tXSe6sQAUBxupQ"
    )


def test_the_key_is_replaced_in_place_or_added_and_nothing_else_changes() -> None:
    original = "A=1\nDASHBOARD_ADMIN_PASSWORD_HASH=\nB=2\n"

    assert script.with_hash(original, "H") == "A=1\nDASHBOARD_ADMIN_PASSWORD_HASH=H\nB=2\n"
    assert script.with_hash("A=1\n", "H") == "A=1\nDASHBOARD_ADMIN_PASSWORD_HASH=H\n"
    assert script.with_hash("A=1", "H") == "A=1\nDASHBOARD_ADMIN_PASSWORD_HASH=H\n"
    assert script.with_hash("", "H") == "DASHBOARD_ADMIN_PASSWORD_HASH=H\n"
    assert script.with_hash("X=DASHBOARD_ADMIN_PASSWORD_HASH=no\n", "H").count("=H") == 1


def test_it_writes_the_file_privately_and_prints_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    env.write_text("KEEP=this\n")
    env.chmod(0o644)
    monkeypatch.setenv("DASH_PW", PASSWORD)

    script.main(["--env", str(env), "--password-env", "DASH_PW"])

    written = env.read_text()
    assert written.startswith("KEEP=this\nDASHBOARD_ADMIN_PASSWORD_HASH=scrypt:")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    shown = capsys.readouterr()
    assert PASSWORD not in shown.out + shown.err
    assert written.split("=", 2)[2].strip() not in shown.out, "the hash is not printed either"
    assert [path.name for path in tmp_path.iterdir()] == [".env"], "no temporary file is left"


def test_a_short_or_missing_password_is_refused_and_the_file_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("KEEP=this\n")
    monkeypatch.setenv("DASH_PW", "short")

    with pytest.raises(SystemExit, match="at least"):
        script.main(["--env", str(env), "--password-env", "DASH_PW"])
    monkeypatch.setenv("DASH_PW", "x" * 513)
    with pytest.raises(SystemExit, match="at most"):
        script.main(["--env", str(env), "--password-env", "DASH_PW"])
    monkeypatch.delenv("DASH_PW")
    with pytest.raises(SystemExit, match="is not set"):
        script.main(["--env", str(env), "--password-env", "DASH_PW"])

    assert env.read_text() == "KEEP=this\n"
