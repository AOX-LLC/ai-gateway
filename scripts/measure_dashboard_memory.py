#!/usr/bin/env python3
"""Measure the dashboard container's memory under the loads that matter, and say what limit they
imply.

    DASHBOARD_PASSWORD=... python3 scripts/measure_dashboard_memory.py [--rounds 3]

For the demo stack (scripts/run_dashboard_demo.sh): the container is sampled once a second (the
cgroup's current memory, read from inside it) through four loads, repeated for each round, and the
cgroup's own recorded peak is read at the end:

  1. first load: the sign-in (a scrypt check takes 32 MiB) and the first overview, on a fresh start;
  2. ten concurrent loads of the overview, in the dark theme and in the light;
  3. the 7-day and 30-day ranges, concurrently (the heaviest queries);
  4. a sign-in burst: thirty concurrent attempts with a wrong password (the throttle answers most).

The limit it suggests is 1.5 times the highest peak seen (rounded up to 16 MiB, at least 128), and
the Node heap 60% of that. It prints the numbers and changes no file or setting; it does restart the
dashboard container before each round (so each starts cold), and refuses any container that is not
the demo project's. The password is read from the environment and never printed."""

import argparse
import contextlib
import http.client
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

COOKIE = "__Host-aig_session"
DEMO_PROJECT = "ai-gateway-demo"


def docker(*args: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed command, arguments from this script
        ["docker", *args],  # noqa: S607 - docker on PATH, as everywhere else here
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


class Sampler(threading.Thread):
    """Reads the container's current memory (bytes) once a second until stopped."""

    def __init__(self, container: str) -> None:
        super().__init__(daemon=True)
        self.container = container
        self.samples: list[int] = []
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            with contextlib.suppress(subprocess.CalledProcessError, ValueError):
                current = docker("exec", self.container, "cat", "/sys/fs/cgroup/memory.current")
                self.samples.append(int(current))
            self._stop_event.wait(max(0.0, 1.0 - (time.monotonic() - started)))

    def stop(self) -> list[int]:
        self._stop_event.set()
        self.join(timeout=5)
        return self.samples


def request(
    host: str,
    method: str,
    path: str,
    *,
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
    body: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(host, timeout=60)
    sent = {"Host": host, **(headers or {})}
    payload = None
    if cookie:
        sent["Cookie"] = cookie
    if body is not None:
        payload = urllib.parse.urlencode(body).encode()
        sent["Content-Type"] = "application/x-www-form-urlencoded"
        sent["Origin"] = f"http://{host}"
    connection.request(method, path, body=payload, headers=sent)
    response = connection.getresponse()
    data = response.read()
    result = (response.status, {k.lower(): v for k, v in response.getheaders()}, data)
    connection.close()
    return result


def sign_in(host: str, password: str) -> str:
    status, headers, _ = request(host, "POST", "/api/signin", body={"password": password})
    cookie = headers.get("set-cookie", "")
    if status != 303 or not cookie.startswith(f"{COOKIE}="):
        sys.exit(
            f"measure: sign-in failed ({status}); is this the demo stack, with its own password?"
        )
    return cookie.split(";", 1)[0]


def wait_healthy(container: str) -> None:
    for _ in range(60):
        if docker("inspect", "-f", "{{.State.Health.Status}}", container) == "healthy":
            return
        time.sleep(1)
    sys.exit("measure: the dashboard did not become healthy")


def mib(value: float) -> float:
    return round(value / 1024 / 1024, 1)


def scenario(container: str, name: str, work: object) -> dict[str, object]:
    sampler = Sampler(container)
    sampler.start()
    time.sleep(1.2)
    started = time.monotonic()
    work()  # type: ignore[operator]
    time.sleep(1.2)
    samples = sampler.stop()
    return {
        "scenario": name,
        "seconds": round(time.monotonic() - started, 1),
        "samples": len(samples),
        "peak_mib": mib(max(samples)) if samples else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--container", help="default: the compose project's dashboard container")
    parser.add_argument("--host", default="127.0.0.1:4400")
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        sys.exit("measure: set DASHBOARD_PASSWORD (the demo stack's)")
    container = args.container or docker(
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={DEMO_PROJECT}",
        "--filter",
        "label=com.docker.compose.service=dashboard",
    )
    if not container:
        sys.exit("measure: no dashboard container of the demo stack (is it up?)")
    project = docker(
        "inspect", "-f", '{{index .Config.Labels "com.docker.compose.project"}}', container
    )
    if project != DEMO_PROJECT:
        sys.exit(f"measure: {container[:12]} belongs to project {project!r}, not {DEMO_PROJECT}")
    limit = int(docker("inspect", "-f", "{{.HostConfig.Memory}}", container))
    results: list[dict[str, object]] = []

    for round_number in range(1, args.rounds + 1):
        docker("restart", container)
        wait_healthy(container)

        def first_load() -> None:
            cookie = sign_in(args.host, password)
            request(args.host, "GET", "/", cookie=cookie)

        results.append({"round": round_number, **scenario(container, "first load", first_load)})
        session = sign_in(args.host, password)

        def pages(paths: list[str], themes: list[str], cookie: str = session) -> None:
            def one(job: tuple[str, str]) -> None:
                path, theme = job
                status, _, _ = request(
                    args.host, "GET", path, cookie=f"{cookie}; aig_theme={theme}"
                )
                request(
                    args.host,
                    "GET",
                    "/api/live?range=" + (path.split("range=")[1] if "range=" in path else "24h"),
                    cookie=cookie,
                )
                if status != 200:
                    sys.exit(f"measure: {path} answered {status}")

            jobs = [(path, theme) for path in paths for theme in themes]
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                list(pool.map(one, jobs))

        results.append(
            {
                "round": round_number,
                **scenario(
                    container,
                    "10 concurrent loads, both themes",
                    lambda: pages(["/"] * 5, ["dark", "light"]),
                ),
            }
        )
        results.append(
            {
                "round": round_number,
                **scenario(
                    container,
                    "7-day and 30-day ranges",
                    lambda: pages(["/?range=7d", "/?range=30d"] * 3, ["dark"]),
                ),
            }
        )

        def burst() -> None:
            with ThreadPoolExecutor(max_workers=30) as pool:
                list(
                    pool.map(
                        lambda _: request(
                            args.host, "POST", "/api/signin", body={"password": "not-the-password"}
                        ),
                        range(30),
                    )
                )

        results.append(
            {"round": round_number, **scenario(container, "sign-in burst (30 attempts)", burst)}
        )

    peak_file = docker("exec", container, "cat", "/sys/fs/cgroup/memory.peak")
    sampled = max(float(str(r["peak_mib"])) for r in results if r["peak_mib"] is not None)
    cgroup_peak = mib(int(peak_file))
    highest = max(sampled, cgroup_peak)
    suggested = max(128, math.ceil(1.5 * highest / 16) * 16)
    report = {
        "container": container[:12],
        "current_limit_mib": mib(limit) if limit else None,
        "rounds": args.rounds,
        "scenarios": results,
        "highest_sampled_mib": sampled,
        "cgroup_peak_since_last_restart_mib": cgroup_peak,
        "highest_mib": highest,
        "suggested_mem_limit_mib": suggested,
        "suggested_node_heap_mib": math.floor(0.6 * suggested),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
