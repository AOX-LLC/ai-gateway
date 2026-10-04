"""Every string the classifier must judge has a committed recording, and none is missing.

The attack corpus, the benign look-alikes, the 09 story and everything the seeds, scenarios and
simulator write are replayed against `config/recordings/`. A miss fails here: in replay mode a miss
is recorded as `unclassified` and never as clean, so a missing recording is a hole in the demo, not
a pass. Re-record with `scripts/record_classifier.py` after changing the prompt, the corpus or the
seeds. Harborline Supply Co. is fictional.
"""

import sys
from pathlib import Path

import pytest
from aox_agent_core import AgentClient, Tier, load_config
from aox_agent_core.errors import ReplayMissError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import classifier_corpus as corpus  # noqa: E402
from ai_gateway.classifier.prompt import JUDGE, Judgement, prepare  # noqa: E402

pytestmark = pytest.mark.anyio


def _client() -> AgentClient:
    return AgentClient(
        load_config(ROOT / "config" / "agent-core.toml", environ={"AGENT_CORE_MODE": "replay"})
    )


async def _misses(units: list[corpus.CorpusUnit]) -> list[str]:
    client = _client()
    missing = []
    for unit in units:
        try:
            await client.call(
                JUDGE,
                inputs={"surface": unit.surface, "text": prepare(unit.text)},
                output=Judgement,
                tier=Tier.SMALL,
                max_tokens=64,
            )
        except ReplayMissError:
            missing.append(f"{unit.source} {unit.item_id or ''} [{unit.surface}]")
    return missing


@pytest.mark.parametrize("source", ["attacks.toml", "benign.toml", "story_09.toml"])
async def test_every_corpus_string_has_a_recording(source: str) -> None:
    units = [u for u in corpus.must_cover() if u.source == source]
    assert units, f"no units in {source}: the corpus did not load"

    assert await _misses(units) == []


async def test_every_seeded_and_scripted_write_has_a_recording() -> None:
    units = corpus.everything()
    assert len(units) > len(corpus.must_cover())

    assert await _misses(units) == []
