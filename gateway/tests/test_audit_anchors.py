"""Anchors: why the hash chain alone is not enough, and what an anchor catches."""

import argparse
import json
import os
import stat
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from aox_agent_core.audit import (
    AuditEvent,
    AuditHead,
    SQLAuditLog,
    UnsealedAuditRecord,
    compute_record_hash,
)
from aox_agent_core.errors import AuditIntegrityError
from aox_agent_core.storage import open_database
from pydantic import SecretStr

from ai_gateway.admin.cli import _audit_anchor, _audit_verify
from ai_gateway.policy import policy_url
from ai_gateway.policy.anchors import (
    Anchor,
    AnchorFileError,
    append_anchor,
    read_anchors,
    verify_with_anchors,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def _log(url: str) -> SQLAuditLog:
    return SQLAuditLog(open_database(SecretStr(policy_url(url))))


async def _append(url: str, count: int) -> None:
    log = _log(url)
    for number in range(count):
        await log.append(
            AuditEvent(
                action="gateway.tool_call",
                actor_id=f"client:{uuid4()}",
                payload={"request_id": str(uuid4()), "outcome": "forwarded", "n": number},
            )
        )


async def _rewrite_and_rebuild_the_chain(owner_url: str, from_seq: int) -> None:
    """What a person with the owner's rights can do: change a record and recompute every hash after
    it, so that the chain still verifies."""
    async with await psycopg.AsyncConnection.connect(
        policy_url(owner_url), autocommit=True
    ) as connection:
        await connection.execute("ALTER TABLE agent_core_audit DISABLE TRIGGER USER")
        try:
            cursor = await connection.execute(
                "SELECT seq, event_id, occurred_at, action, actor_id, subject_id, payload,"
                " run_context FROM agent_core_audit ORDER BY seq"
            )
            rows = await cursor.fetchall()
            previous = "0" * 64
            for seq, event_id, occurred_at, action, actor_id, subject_id, payload, context in rows:
                if seq == from_seq:
                    payload = json.dumps(
                        {**json.loads(payload), "outcome": "blocked"}, sort_keys=True
                    )
                unsealed = UnsealedAuditRecord.model_validate(
                    {
                        "seq": seq,
                        "event_id": event_id,
                        "occurred_at": occurred_at,
                        "action": action,
                        "actor_id": actor_id,
                        "subject_id": subject_id,
                        "payload": json.loads(payload),
                        "run_context": json.loads(context) if context else None,
                        "prev_hash": previous,
                    }
                )
                previous = compute_record_hash(unsealed)
                await connection.execute(
                    "UPDATE agent_core_audit SET payload = %s, prev_hash = %s, record_hash = %s"
                    " WHERE seq = %s",
                    (json.dumps(unsealed.payload, sort_keys=True, separators=(",", ":")),
                     unsealed.prev_hash, previous, seq),
                )  # fmt: skip
        finally:
            await connection.execute("ALTER TABLE agent_core_audit ENABLE TRIGGER USER")


# --- the anchor file ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_anchor_is_appended_to_a_private_file_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / "anchors.jsonl"
    first = append_anchor(path, AuditHead(seq=3, record_hash="a" * 64))
    second = append_anchor(path, AuditHead(seq=9, record_hash="b" * 64))

    assert read_anchors(path) == [first, second]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().count("\n") == 2


def test_an_anchor_file_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("")
    link = tmp_path / "anchors.jsonl"
    os.symlink(target, link)

    with pytest.raises(OSError):  # noqa: PT011 - ELOOP, from O_NOFOLLOW
        append_anchor(link, AuditHead(seq=1, record_hash="a" * 64))


def test_an_anchor_file_that_others_can_write_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "anchors.jsonl"
    append_anchor(path, AuditHead(seq=1, record_hash="a" * 64))
    path.chmod(0o666)

    with pytest.raises(AnchorFileError, match="written by others"):
        read_anchors(path)


def test_an_anchor_file_that_is_a_symlink_is_not_read(tmp_path: Path) -> None:
    real = tmp_path / "real.jsonl"
    append_anchor(real, AuditHead(seq=1, record_hash="a" * 64))
    link = tmp_path / "anchors.jsonl"
    os.symlink(real, link)

    with pytest.raises(AnchorFileError, match="cannot read"):
        read_anchors(link)


async def test_an_anchor_of_the_empty_log_must_hold_the_genesis_hash(
    policy: None, policy_auditor_url: str
) -> None:
    with pytest.raises(AuditIntegrityError, match="genesis"):
        await verify_with_anchors(_log(policy_auditor_url), [Anchor(0, "f" * 64, "now")])


def test_a_file_that_is_not_an_anchor_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "anchors.jsonl"
    path.write_text('{"seq": 1}\n')

    with pytest.raises(AnchorFileError, match="line 1"):
        read_anchors(path)
    with pytest.raises(AnchorFileError, match="cannot read"):
        read_anchors(tmp_path / "missing.jsonl")


# --- verifying ----------------------------------------------------------------------------------


async def test_an_untouched_log_verifies_against_every_anchor_it_has_grown_past(
    policy: None, policy_gateway_url: str, policy_auditor_url: str, tmp_path: Path
) -> None:
    anchors_file = tmp_path / "anchors.jsonl"
    auditor = _log(policy_auditor_url)
    await _append(policy_gateway_url, 3)
    append_anchor(anchors_file, await auditor.head())
    await _append(policy_gateway_url, 4)
    append_anchor(anchors_file, await auditor.head())
    await _append(policy_gateway_url, 2)

    head = await verify_with_anchors(auditor, read_anchors(anchors_file))

    assert head.seq == 9


async def test_a_rewritten_record_with_a_rebuilt_chain_passes_the_chain_and_fails_the_anchor(
    policy: None,
    policy_gateway_url: str,
    policy_auditor_url: str,
    test_database_url: str,
    tmp_path: Path,
) -> None:
    anchors_file = tmp_path / "anchors.jsonl"
    auditor = _log(policy_auditor_url)
    await _append(policy_gateway_url, 6)
    append_anchor(anchors_file, await auditor.head())
    await _rewrite_and_rebuild_the_chain(test_database_url, from_seq=3)

    assert (await auditor.verify()).seq == 6, (
        "the chain alone cannot see it: the hashes were rebuilt"
    )
    with pytest.raises(AuditIntegrityError):
        await verify_with_anchors(auditor, read_anchors(anchors_file))


async def test_a_log_cut_short_fails_the_anchor_that_remembers_more(
    policy: None,
    policy_gateway_url: str,
    policy_auditor_url: str,
    test_database_url: str,
    tmp_path: Path,
) -> None:
    anchors_file = tmp_path / "anchors.jsonl"
    auditor = _log(policy_auditor_url)
    await _append(policy_gateway_url, 6)
    append_anchor(anchors_file, await auditor.head())
    async with await psycopg.AsyncConnection.connect(
        policy_url(test_database_url), autocommit=True
    ) as connection:
        await connection.execute("ALTER TABLE agent_core_audit DISABLE TRIGGER USER")
        await connection.execute("DELETE FROM agent_core_audit WHERE seq > 4")
        await connection.execute("ALTER TABLE agent_core_audit ENABLE TRIGGER USER")

    assert (await auditor.verify()).seq == 4, "a shorter chain is still a valid chain"
    with pytest.raises(AuditIntegrityError):
        await verify_with_anchors(auditor, read_anchors(anchors_file))


async def test_two_anchors_that_name_the_same_record_differently_fail_verification(
    policy: None, policy_gateway_url: str, policy_auditor_url: str, tmp_path: Path
) -> None:
    """A later anchor must not hide an earlier one: re-anchoring after a rewrite would otherwise
    replace the evidence of the rewrite with the rewritten log's own hash."""
    anchors_file = tmp_path / "anchors.jsonl"
    auditor = _log(policy_auditor_url)
    await _append(policy_gateway_url, 4)
    head = await auditor.head()
    append_anchor(anchors_file, head)
    append_anchor(anchors_file, AuditHead(seq=head.seq, record_hash="f" * 64))

    with pytest.raises(AuditIntegrityError, match="disagree"):
        await verify_with_anchors(auditor, read_anchors(anchors_file))


async def test_a_log_that_fails_its_anchors_is_not_anchored_again(
    policy: None,
    policy_gateway_url: str,
    policy_auditor_url: str,
    test_database_url: str,
    tmp_path: Path,
) -> None:
    anchors_file = tmp_path / "anchors.jsonl"
    await _append(policy_gateway_url, 6)
    await _audit_anchor(policy_auditor_url, argparse.Namespace(file=anchors_file))
    await _rewrite_and_rebuild_the_chain(test_database_url, from_seq=3)
    before = anchors_file.read_text()

    with pytest.raises(AuditIntegrityError):
        await _audit_anchor(policy_auditor_url, argparse.Namespace(file=anchors_file))

    assert anchors_file.read_text() == before, "the rewritten head must not become an anchor"


@pytest.mark.parametrize(
    "line",
    [
        '{"seq": -1, "record_hash": "' + "a" * 64 + '", "at": "now"}',
        '{"seq": 1, "record_hash": "not a hash", "at": "now"}',
        '{"seq": "1", "record_hash": "' + "a" * 64 + '", "at": "now"}',
        '{"seq": 1, "record_hash": "' + "a" * 64 + '\\n", "at": "now"}',  # a trailing newline
    ],
)
def test_an_anchor_with_an_impossible_value_is_refused(tmp_path: Path, line: str) -> None:
    path = tmp_path / "anchors.jsonl"
    path.write_text(line + "\n")

    with pytest.raises(AnchorFileError, match="line 1"):
        read_anchors(path)


async def test_an_empty_log_and_no_anchors_verify(policy: None, policy_auditor_url: str) -> None:
    head = await verify_with_anchors(_log(policy_auditor_url), [])

    assert head.seq == 0


# --- the commands -------------------------------------------------------------------------------


async def test_the_commands_anchor_the_log_and_verify_it(
    policy: None,
    policy_gateway_url: str,
    policy_auditor_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    anchors_file = tmp_path / "anchors.jsonl"
    await _append(policy_gateway_url, 3)

    await _audit_anchor(policy_auditor_url, argparse.Namespace(file=anchors_file))
    await _audit_verify(policy_auditor_url, argparse.Namespace(anchors=anchors_file))

    output = capsys.readouterr().out
    assert "anchored record 3" in output
    assert "ok: 3 records chain correctly and match 1 anchors" in output
