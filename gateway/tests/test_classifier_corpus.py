"""The corpus is well formed: every item names the layers that should catch it.

The rule, in `config/classifier_corpus/README.md` and written before it was applied: the classifier
is an expected catcher of an item only if its text contains an instruction directed at a model.
Harborline Supply Co. is fictional.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import classifier_corpus as corpus  # noqa: E402


def test_every_item_is_scored_by_some_layer_or_is_clean() -> None:
    units = corpus.must_cover()

    assert len(units) == 39
    for unit in units:
        assert bool(unit.catchers) == (unit.expect == "injection"), unit.item_id


def test_the_data_only_items_are_not_the_classifiers() -> None:
    by_id = {u.item_id: u for u in corpus.must_cover()}

    for item_id in ("export-via-ticket", "write-bulk-drip", "ticket-injection-export"):
        assert by_id[item_id].expect == "injection"  # the overall label is unchanged
        assert by_id[item_id].catchers == ("egress", "canary")
    assert sum("classifier" in u.catchers for u in by_id.values()) == 21


def test_a_missing_or_unknown_catcher_is_refused() -> None:
    item = {"id": "x", "expect": "injection"}

    with pytest.raises(ValueError, match="catchers"):
        corpus._catchers("attacks.toml", item)
    with pytest.raises(ValueError, match="catchers"):
        corpus._catchers("attacks.toml", {**item, "catchers": ["magic"]})
    with pytest.raises(ValueError, match="hostile"):
        corpus._catchers("attacks.toml", {**item, "catchers": []})
