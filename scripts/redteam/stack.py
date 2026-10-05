"""Docker Compose for the scorecard: every command under the shared lock where there is one, one
command at a time.

`flock ~/portfolio-projects/.locks/docker docker compose ...` (a CI runner has no such lock and
takes none): the lock is held for one stack
command and released before the next, never across a whole run, so another project's stack run waits
for a command and not for the scorecard. Commands print nothing of the environment they are given
(it holds the lab credential), and a failure shows the tail of the command's own output with any
token shaped text removed. Harborline Supply Co. is fictional.
"""

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import anyio

APPROVER_READY = "READY: lab approver"
"""The line the lab approver prints once its login is checked: the runner waits for it."""
PROTECTED_PROJECTS = frozenset({"ai-gateway", "ai-gateway-demo", "ai-gateway-verify"})


def approver_ready_line(decision: str) -> str:
    """What the lab approver says when it is connected and about to `approve` or `reject`."""
    return f"{APPROVER_READY} lab-approver will {decision} every pending write"


LOCK = Path(
    os.environ.get("DOCKER_LOCK") or Path.home() / "portfolio-projects" / ".locks" / "docker"
)
"""The workstation's shared Docker lock (the same file `release_smoke.sh` takes, and `DOCKER_LOCK`
moves it). Where there is none, as on a CI runner, no one else shares Docker: nothing is locked."""
_TOKEN = re.compile(r"aig_[a-z0-9]+_[A-Za-z0-9_-]{20,}")


def redact(text: str) -> str:
    """The text with anything shaped like a gateway token replaced, so no output can leak one."""
    return _TOKEN.sub("[token]", text)


class StackError(RuntimeError):
    pass


def lock_prefix() -> list[str]:
    """`flock <lock>` where the shared lock's directory exists, and nothing where it does not."""
    return ["flock", str(LOCK)] if LOCK.parent.is_dir() else []


async def run(
    command: Sequence[str],
    env: Mapping[str, str],
    cwd: Path,
    *,
    locked: bool = False,
    stdin: bytes | None = None,
) -> str:
    """Run a command and return its output; a non-zero exit is a StackError naming the command and
    the redacted tail of its output. `locked` runs it under the shared Docker lock."""
    full = [*lock_prefix(), *command] if locked else list(command)
    result = await anyio.run_process(
        full, env=dict(env), cwd=cwd, check=False, input=stdin, stderr=-2
    )
    output = result.stdout.decode("utf-8", "replace")
    if result.returncode != 0:
        tail = redact(output[-1500:])
        raise StackError(f"`{' '.join(command[:4])}` exited {result.returncode}:\n{tail}")
    return output


class Stack:
    """One Compose project (never the real `ai-gateway` one) with the lab profile."""

    def __init__(self, repo: Path, project: str, env: Mapping[str, str]) -> None:
        if project in PROTECTED_PROJECTS:
            raise ValueError(f"{project} holds data: the scorecard removes its volume at the end")
        self.repo = repo
        self.project = project
        self.env = {**env, "COMPOSE_PROJECT_NAME": project}

    def command(self, *args: str) -> list[str]:
        """`docker compose -p <project> --profile lab ...`: the project is always passed as a flag,
        never left to the environment."""
        return ["docker", "compose", "-p", self.project, "--profile", "lab", *args]

    async def compose(self, *args: str, extra: Mapping[str, str] | None = None) -> str:
        env = {**self.env, **(extra or {})}
        return await run(self.command(*args), env, self.repo, locked=True)

    async def set_lab_phase(self, phase: str) -> None:
        """Put the lab upstream in a phase without restarting it (counts start afresh)."""
        script = (
            "import os,urllib.request;"
            f"r=urllib.request.Request('http://127.0.0.1:4413/phase/{phase}',method='POST',"
            "headers={'Authorization':'Bearer '+os.environ['LAB_SERVICE_TOKEN']});"
            "urllib.request.urlopen(r,timeout=10).read()"
        )
        await self.compose("exec", "-T", "lab-upstream", "python", "-c", script)

    async def lab_effects(self) -> dict[str, Any]:
        """The lab upstream's own count of what it executed, read from inside the network (it
        publishes no port). Counts only, never a value."""
        script = (
            "import json,os,urllib.request;"
            "r=urllib.request.Request('http://127.0.0.1:4413/effects',"
            "headers={'Authorization':'Bearer '+os.environ['LAB_SERVICE_TOKEN']});"
            "print(json.dumps(json.loads(urllib.request.urlopen(r,timeout=10).read())))"
        )
        out = await self.compose("exec", "-T", "lab-upstream", "python", "-c", script)
        snapshot: dict[str, Any] = json.loads(out.strip().splitlines()[-1])
        return snapshot

    async def wait_for_approver(self, decision: str, attempts: int = 60) -> None:
        """Wait until the lab approver says it is connected and will `decision` every write."""
        logs = ""
        for _ in range(attempts):
            logs = await self.compose("logs", "--no-color", "lab-approver")
            if approver_ready_line(decision) in logs:
                return
            await anyio.sleep(2)
        tail = redact("\n".join(logs.splitlines()[-6:]))
        raise StackError(f"the lab approver never said it was ready; its last output:\n{tail}")


def read_env_file(path: Path) -> dict[str, str]:
    """A `.env` file as a dict. The values are secrets: nothing here prints them."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def clean_environment() -> dict[str, str]:
    """The process environment with nothing that could turn replay into a billed call."""
    env = dict(os.environ)
    env["AGENT_CORE_MODE"] = "replay"
    env["AGENT_CORE_ANTHROPIC_API_KEY"] = ""
    return env
