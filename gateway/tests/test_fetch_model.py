"""scripts/fetch_model.py: pinned downloads, verified, idempotent."""

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fetch_model.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fetch_model", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fetch_model"] = module
    spec.loader.exec_module(module)
    return module


fetch_model = _load()

CONTENT = {"a.json": b"alpha", "b.bin": b"bravo" * 1000}
FILES = {name: hashlib.sha256(body).hexdigest() for name, body in CONTENT.items()}


class _Server:
    """A stand-in for the download: serves CONTENT, can corrupt or fail a file."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.corrupt: set[str] = set()
        self.fail_first: set[str] = set()

    def __call__(self, url: str, target: Path) -> None:
        name = url.rsplit("/", 1)[1]
        self.calls.append(name)
        if name in self.fail_first and self.calls.count(name) == 1:
            raise OSError("connection reset")
        body = b"tampered" if name in self.corrupt else CONTENT[name]
        target.write_bytes(body)


def _fetch(dest: Path, server: _Server) -> list[str]:
    result: list[str] = fetch_model.fetch(
        dest, FILES, "https://example.test/model", server, retry_delay_s=0
    )
    return result


def test_the_pinned_list_covers_the_model_files_and_the_licence() -> None:
    assert set(fetch_model.FILES) == {
        "config.json", "model.safetensors", "modules.json", "tokenizer.json",
        "tokenizer_config.json", "special_tokens_map.json", "README.md",
    }  # fmt: skip
    assert fetch_model.REVISION in fetch_model.BASE_URL
    assert all(len(digest) == 64 for digest in fetch_model.FILES.values())


def test_every_file_is_downloaded_and_verified(tmp_path: Path) -> None:
    server = _Server()

    fetched = _fetch(tmp_path / "model", server)

    assert fetched == ["a.json", "b.bin"]
    assert (tmp_path / "model" / "b.bin").read_bytes() == CONTENT["b.bin"]
    assert not list((tmp_path / "model").glob("*.part"))


def test_a_second_run_downloads_nothing(tmp_path: Path) -> None:
    _fetch(tmp_path, _Server())
    again = _Server()

    assert _fetch(tmp_path, again) == []
    assert again.calls == []


def test_a_damaged_file_is_fetched_again(tmp_path: Path) -> None:
    _fetch(tmp_path, _Server())
    (tmp_path / "a.json").write_bytes(b"damaged")
    again = _Server()

    assert _fetch(tmp_path, again) == ["a.json"]
    assert (tmp_path / "a.json").read_bytes() == CONTENT["a.json"]


def test_a_download_that_does_not_match_its_hash_is_refused_and_removed(tmp_path: Path) -> None:
    server = _Server()
    server.corrupt = {"b.bin"}

    with pytest.raises(fetch_model.ModelFileError, match=r"b\.bin"):
        _fetch(tmp_path, server)

    assert not (tmp_path / "b.bin").exists()
    assert not list(tmp_path.glob("*.part"))
    assert server.calls.count("b.bin") == 1


def test_a_failed_download_is_retried(tmp_path: Path) -> None:
    server = _Server()
    server.fail_first = {"a.json"}

    assert _fetch(tmp_path, server) == ["a.json", "b.bin"]
    assert server.calls.count("a.json") == 2


def test_a_plain_http_url_is_refused() -> None:
    with pytest.raises(fetch_model.ModelFileError, match="HTTPS"):
        fetch_model._download("http://example.test/x", Path("unused"))
