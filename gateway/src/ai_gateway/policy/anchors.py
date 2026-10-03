"""Anchors for the audit log: heads kept where the log's writers cannot reach.

The hash chain proves nothing by itself: whoever can rewrite rows can rebuild every hash after
them. An anchor is the chain's head (its sequence number and hash) written to a file outside the
database, so a later rewrite or truncation no longer matches. `gateway-admin audit-anchor` appends
one; `gateway-admin audit-verify --anchors FILE` walks the chain and checks every anchor against it.

Keep the file on a different host, or at least a different account, from the database, and take an
anchor on a schedule (cron is enough). An anchor only protects what came before it.
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aox_agent_core.audit import AuditHead, SQLAuditLog
from aox_agent_core.errors import AuditIntegrityError


class AnchorFileError(ValueError):
    """The anchor file is unreadable or not an anchor file."""


@dataclass(frozen=True)
class Anchor:
    seq: int
    record_hash: str
    taken_at: str

    @property
    def head(self) -> AuditHead:
        return AuditHead(seq=self.seq, record_hash=self.record_hash)


def append_anchor(path: Path, head: AuditHead, *, now: datetime | None = None) -> Anchor:
    """Append the head to the anchor file (one JSON object a line), created readable by its owner
    only. The file is only ever appended to."""
    anchor = Anchor(head.seq, head.record_hash, (now or datetime.now(UTC)).isoformat())
    line = json.dumps({"seq": anchor.seq, "record_hash": anchor.record_hash, "at": anchor.taken_at})
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return anchor


def read_anchors(path: Path) -> list[Anchor]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AnchorFileError(f"cannot read the anchor file {path}: {error.strerror}") from error
    anchors = []
    for number, line in enumerate(lines, start=1):
        try:
            raw = json.loads(line)
            anchors.append(Anchor(int(raw["seq"]), str(raw["record_hash"]), str(raw["at"])))
        except (ValueError, KeyError, TypeError) as error:
            raise AnchorFileError(f"{path} line {number} is not an anchor") from error
    return anchors


async def verify_with_anchors(log: SQLAuditLog, anchors: list[Anchor]) -> AuditHead:
    """Walk the whole chain, then check every anchor against it. Returns the chain's head.

    Raises AuditIntegrityError if a record's hash or link is wrong, if the log is shorter than an
    anchor says, or if the record at an anchor's sequence number is not the one that was
    anchored."""
    taken = [anchor for anchor in anchors if anchor.seq > 0]
    newest = max(taken, key=lambda anchor: anchor.seq, default=None)
    head = await log.verify(expected_head=newest.head if newest else None)
    wanted = {anchor.seq: anchor for anchor in taken}
    async for record in log.iter_records():
        anchor = wanted.pop(record.seq, None)
        if anchor is not None and record.record_hash != anchor.record_hash:
            raise AuditIntegrityError(
                f"record {record.seq} is not the one anchored at {anchor.taken_at}"
            )
    if wanted:
        raise AuditIntegrityError(
            f"the log has no record {min(wanted)} that an anchor names: it was cut short"
        )
    return head
