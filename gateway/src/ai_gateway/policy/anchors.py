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
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aox_agent_core.audit import GENESIS_HASH, AuditHead, SQLAuditLog
from aox_agent_core.errors import AuditIntegrityError

_SHA256 = re.compile(r"[0-9a-f]{64}")


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
    """The anchors in the file. A symlink is refused, as it is when appending, and so is a file
    that others can write to: whoever can edit it can delete the anchors that would show a
    rewrite."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            status = os.fstat(handle.fileno())
            if status.st_mode & 0o022:
                raise AnchorFileError(f"{path} can be written by others; chmod go-w it")
            if status.st_uid not in {os.getuid(), 0}:
                raise AnchorFileError(f"{path} belongs to another account")
            lines = handle.read().splitlines()
    except OSError as error:
        raise AnchorFileError(f"cannot read the anchor file {path}: {error.strerror}") from error
    anchors = []
    for number, line in enumerate(lines, start=1):
        try:
            raw = json.loads(line)
            seq, record_hash, taken_at = raw["seq"], raw["record_hash"], raw["at"]
            if (
                not isinstance(seq, int)
                or isinstance(seq, bool)
                or seq < 0
                or not isinstance(record_hash, str)
                or _SHA256.fullmatch(record_hash) is None
                or not isinstance(taken_at, str)
            ):
                raise ValueError("not an anchor")
            anchors.append(Anchor(seq, record_hash, taken_at))
        except (ValueError, KeyError, TypeError) as error:
            raise AnchorFileError(f"{path} line {number} is not an anchor") from error
    return anchors


async def verify_with_anchors(log: SQLAuditLog, anchors: list[Anchor]) -> AuditHead:
    """Walk the whole chain, then check every anchor against it. Returns the chain's head.

    Raises AuditIntegrityError if a record's hash or link is wrong, if the log is shorter than an
    anchor says, or if the record at an anchor's sequence number is not the one that was
    anchored."""
    for anchor in anchors:
        if anchor.seq == 0 and anchor.record_hash != GENESIS_HASH:
            raise AuditIntegrityError("an anchor of the empty log does not hold the genesis hash")
    taken = [anchor for anchor in anchors if anchor.seq > 0]
    wanted: dict[int, list[Anchor]] = defaultdict(list)
    for anchor in taken:
        wanted[anchor.seq].append(anchor)
    for seq, same_record in wanted.items():
        # Every anchor is checked below; two that name one record differently are already proof
        # that the log changed between them, whatever it holds now.
        if len({anchor.record_hash for anchor in same_record}) > 1:
            raise AuditIntegrityError(f"anchors disagree about record {seq}: the log was rewritten")
    newest = max(taken, key=lambda anchor: anchor.seq, default=None)
    head = await log.verify(expected_head=newest.head if newest else None)
    async for record in log.iter_records():
        for anchor in wanted.pop(record.seq, []):
            if record.record_hash != anchor.record_hash:
                raise AuditIntegrityError(
                    f"record {record.seq} is not the one anchored at {anchor.taken_at}"
                )
    if wanted:
        raise AuditIntegrityError(
            f"the log has no record {min(wanted)} that an anchor names: it was cut short"
        )
    return head
