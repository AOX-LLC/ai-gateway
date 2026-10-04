"""Remove the arguments of finished approval requests after their retention.

`approvals-purge` runs once, or every `--every` seconds, as the payload purger's login. agent-core
keeps a request's arguments with the request (so an approver is shown what its hash binds) and lets
only the approver side purge them, so this service is separate from the gateway, which never holds
that right. A purge removes the stored payload of a request that is consumed, rejected, cancelled
or expired and finished more than the retention ago, keeps the hash, and writes one
`approval.payload_purged` audit record for each.
"""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import timedelta

import anyio
from aox_agent_core.approvals import Principal, PrincipalKind, SQLApprovalQueue
from pydantic import SecretStr

from ai_gateway.policy import (
    PAYLOAD_RETENTION_DAYS,
    PURGER_PRINCIPAL,
    approval_queue_on,
    policy_url,
)
from ai_gateway.policy.database import BoundedPostgresDatabase

logger = logging.getLogger(__name__)

_URL_ENV = "POLICY_PURGER_DATABASE_URL"
BATCH = 500
_PURGER = Principal(id=PURGER_PRINCIPAL, kind=PrincipalKind.SERVICE)


async def purge_payloads(queue: SQLApprovalQueue, retention: timedelta, batch: int = BATCH) -> int:
    """Purge until a batch comes back short (a request another transaction holds is skipped, and
    the next run takes it). Returns how many payloads were removed."""
    total = 0
    while True:
        purged = await queue.purge_payloads(principal=_PURGER, older_than=retention, limit=batch)
        total += purged
        if purged < batch:
            return total


async def _run(url: str, retention: timedelta, every: int) -> None:
    database = BoundedPostgresDatabase(SecretStr(policy_url(url)), concurrency=1)
    queue = approval_queue_on(database)
    try:
        while True:
            try:
                logger.info(
                    "approval payload purge removed %s", await purge_payloads(queue, retention)
                )
            except Exception:
                # A purge that fails is retried at the next interval; it never stops the service.
                logger.exception("approval payload purge failed")
                if not every:
                    raise
            if not every:
                return
            await anyio.sleep(every)
    finally:
        await database.aclose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="approvals-purge", description=__doc__)
    parser.add_argument("--every", type=int, default=0, metavar="SECONDS", help="0 runs once")
    parser.add_argument("--days", type=int, default=PAYLOAD_RETENTION_DAYS, help="retention")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    url = os.environ.get(_URL_ENV)
    if not url:
        sys.exit(f"approvals-purge: {_URL_ENV} is not set")
    if args.days < 2 or args.every < 0:
        sys.exit("approvals-purge: --days is at least 2 and --every is not negative")
    logging.basicConfig(level=logging.INFO)
    anyio.run(_run, url, timedelta(days=args.days), args.every)


if __name__ == "__main__":
    main()
