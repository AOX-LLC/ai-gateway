"""The release smoke test takes the shared Docker lock for each stack command, never for the whole
script: another project's stack run waits for one command at a time, not for a ten-minute script."""

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release_smoke.sh"
CODE = [
    (number, line.strip())
    for number, line in enumerate(SCRIPT.read_text(encoding="utf-8").splitlines(), start=1)
    if line.strip() and not line.strip().startswith("#")
]


def test_the_script_is_valid_shell() -> None:
    subprocess.run(["/bin/bash", "-n", str(SCRIPT)], check=True)


def test_the_lock_is_a_function_that_takes_one_command() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert re.search(r'^lock\(\) \{ .*flock "\$LOCK" "\$@"', text, re.MULTILINE)
    assert "flock ~/portfolio-projects/.locks/docker" not in text


def test_the_script_never_re_execs_itself_under_flock_or_holds_the_lock_across_commands() -> None:
    for number, line in CODE:
        if line.startswith("lock()"):
            continue
        assert "exec flock" not in line, f"line {number} holds the lock for the whole script"
        assert not re.search(r"flock\b.*\$0", line), f"line {number} holds the lock for the script"


def test_every_stack_command_runs_under_the_lock() -> None:
    stack = re.compile(
        r"(?<![\w-])(docker compose (build|up|down|run|logs|restart|exec)|docker run"
        r"|scripts/(run_harborline_check|run_redteam_check|check_image_has_no_restricted_text)\.sh)"
    )
    found = 0
    for number, line in CODE:
        if line.startswith(("echo ", "lock()")) or not stack.search(line):
            continue
        found += 1
        # `lock docker compose ...`, optionally after VAR=value assignments, in a subshell, a list
        # or an `if`
        assert re.search(r"(^|[(;&|!]\s*|\bif\s+(! )?)(\w+=\S+\s+)*lock\s", line), (
            f"line {number}: {line}"
        )
    assert found >= 6, "the stack commands were not found: the pattern needs updating"


def test_a_build_and_a_run_are_separate_locked_commands() -> None:
    commands = "\n".join(line for _, line in CODE)

    assert "docker compose build" in commands
    assert "up -d --build" not in commands, "one lock around a build and a whole run"
