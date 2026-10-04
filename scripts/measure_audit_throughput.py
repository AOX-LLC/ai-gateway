"""Measure the audit log and the approval queue on this node, as the gateway's own role.

Run against a stack's Postgres (not the test one): it appends fictional records to the audit log of
the database it is pointed at, so use a throwaway stack or a copy of a volume.

    POLICY_GATEWAY_DATABASE_URL=postgresql://policy_gateway:...@127.0.0.1:4402/ai_gateway \
        uv run scripts/measure_audit_throughput.py [--count 50] [--repeat 5]

It reports, in milliseconds: one append at a time, `count` appends at once (what the audit log
serialises), one `append_many` of 100 (what the gateway's recorder writes), and an approval's
submit, repeat-submit and cancel. The numbers go in docs/architecture.md with the date and the
agent-core tag; the earlier ones were taken under a3 on the same kind of node.
"""

import argparse
import asyncio
import os
import statistics
import sys
import time
from uuid import uuid4

from aox_agent_core.approvals import (
    ApprovalRequest,
    Principal,
    PrincipalKind,
    SQLApprovalQueue,
)
from aox_agent_core.audit import AuditEvent
from pydantic import JsonValue, SecretStr

from ai_gateway.policy import approval_queue_on, audit_log_on, policy_url
from ai_gateway.policy.database import BoundedPostgresDatabase


def _event() -> AuditEvent:
    return AuditEvent(
        action="gateway.measure",
        actor_id="measure",
        subject_id="tickets__create_ticket",
        payload={"record_id": str(uuid4()), "note": "fictional, from measure_audit_throughput"},
    )


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


async def _timed(work) -> float:  # type: ignore[no-untyped-def]
    started = time.perf_counter()
    await work()
    return time.perf_counter() - started


async def _submit(
    queue: SQLApprovalQueue, requester: Principal, payload: dict[str, JsonValue]
) -> ApprovalRequest:
    return await queue.submit(
        action="tickets__create_ticket",
        summary="tickets__create_ticket measure",
        payload=payload,
        requested_by=requester,
        required_role="approver",
        ttl_seconds=60,
        include_payload=True,
    )


async def _measure(url: str, count: int, repeat: int) -> None:
    database = BoundedPostgresDatabase(SecretStr(policy_url(url)), concurrency=max(count, 10))
    log = audit_log_on(database)
    queue = approval_queue_on(database)
    requester = Principal(id=f"client:{uuid4()}", kind=PrincipalKind.SERVICE)
    try:
        singles = [await _timed(lambda: log.append(_event())) for _ in range(count)]
        print(
            f"one append at a time ({count}): median {_ms(statistics.median(singles))} ms,"
            f" p95 {_ms(_percentile(singles, 0.95))} ms"
        )
        runs = []
        for _ in range(repeat):

            async def concurrent() -> None:
                await asyncio.gather(*(log.append(_event()) for _ in range(count)))

            runs.append(await _timed(concurrent))
        print(
            f"{count} appends at once, {repeat} runs: min {min(runs):.2f} s,"
            f" median {statistics.median(runs):.2f} s, max {max(runs):.2f} s"
        )
        batches = [
            await _timed(lambda: log.append_many([_event() for _ in range(100)]))
            for _ in range(repeat)
        ]
        print(f"one append_many of 100, {repeat} runs: median {_ms(statistics.median(batches))} ms")

        submitted, repeated, cancelled = [], [], []
        for _ in range(count):
            payload: dict[str, JsonValue] = {
                "arguments": {"n": str(uuid4())},
                "upstream": "measure",
            }
            started = time.perf_counter()
            request = await _submit(queue, requester, payload)
            submitted.append(time.perf_counter() - started)
            started = time.perf_counter()
            await _submit(queue, requester, payload)
            repeated.append(time.perf_counter() - started)
            started = time.perf_counter()
            await queue.cancel(request.id, principal=requester)
            cancelled.append(time.perf_counter() - started)
        for label, values in (
            ("submit (new)", submitted),
            ("submit (repeat of an open one)", repeated),
            ("cancel", cancelled),
        ):
            print(
                f"approval {label} ({count}): median {_ms(statistics.median(values))} ms,"
                f" p95 {_ms(_percentile(values, 0.95))} ms"
            )
    finally:
        await database.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    url = os.environ.get("POLICY_GATEWAY_DATABASE_URL")
    if not url:
        sys.exit("measure_audit_throughput: POLICY_GATEWAY_DATABASE_URL is not set")
    asyncio.run(_measure(url, args.count, args.repeat))


if __name__ == "__main__":
    main()
