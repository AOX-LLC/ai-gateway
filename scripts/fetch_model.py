"""Download the handbook's embedding model into a directory, verified file by file.

    python3 scripts/fetch_model.py --dest /opt/models/potion-base-8M

The model is minishlab/potion-base-8M (MIT licence, about 30 MB), a static model2vec
embedding model, pinned to one Hugging Face revision. Every file is checked against the
SHA-256 in scripts/potion-base-8M.sha256, so a changed or tampered download is refused and
removed. A file that is already present and matches is left alone, so running it again
changes nothing.

Standard library only: the servers image runs it at build time, before any dependency is
installed, so that the running containers never need the network.
"""

import argparse
import hashlib
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

MODEL = "minishlab/potion-base-8M"
REVISION = "bf8b056651a2c21b8d2565580b8569da283cab23"
BASE_URL = f"https://huggingface.co/{MODEL}/resolve/{REVISION}"

HASH_LIST = Path(__file__).with_name("potion-base-8M.sha256")
"""The pinned SHA-256 of every file, in `sha256sum` format. The model card (README.md)
carries the licence (MIT)."""


def read_hash_list(path: Path = HASH_LIST) -> dict[str, str]:
    """File name to SHA-256, from a `sha256sum`-format list."""
    pairs = (line.split() for line in path.read_text(encoding="utf-8").splitlines() if line)
    return {name: digest for digest, name in pairs}


FILES = read_hash_list()

_ATTEMPTS = 3
_TIMEOUT_S = 60
_CHUNK = 1 << 20

Download = Callable[[str, Path], None]


class ModelFileError(Exception):
    """A file could not be fetched, or did not match its pinned hash."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, target: Path) -> None:
    if not url.startswith("https://"):
        raise ModelFileError(f"refusing a non-HTTPS url: {url}")
    with (
        urllib.request.urlopen(url, timeout=_TIMEOUT_S) as response,  # noqa: S310 - checked above
        target.open("wb") as handle,
    ):
        while chunk := response.read(_CHUNK):
            handle.write(chunk)


def fetch(
    dest: Path,
    files: Mapping[str, str] = FILES,
    base_url: str = BASE_URL,
    download: Download = _download,
    retry_delay_s: float = 2.0,
) -> list[str]:
    """Make `dest` hold every file with its pinned hash; return the names fetched now."""
    dest.mkdir(parents=True, exist_ok=True)
    fetched = []
    for name, expected in files.items():
        target = dest / name
        if target.is_file() and sha256_of(target) == expected:
            continue
        _fetch_one(f"{base_url}/{name}", target, expected, download, retry_delay_s)
        fetched.append(name)
    return fetched


def _fetch_one(
    url: str, target: Path, expected: str, download: Download, retry_delay_s: float
) -> None:
    partial = target.with_name(target.name + ".part")
    last_error: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            download(url, partial)
            if sha256_of(partial) != expected:
                raise ModelFileError(f"{target.name}: SHA-256 does not match the pinned value")
        except (OSError, ModelFileError) as error:
            last_error = error
            partial.unlink(missing_ok=True)
            if isinstance(error, ModelFileError) and "SHA-256" in str(error):
                break  # a wrong file will not become right by asking again
            if attempt < _ATTEMPTS:
                time.sleep(retry_delay_s)
            continue
        partial.replace(target)
        return
    raise ModelFileError(f"could not fetch {target.name}: {last_error}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=f"Fetch {MODEL} at revision {REVISION[:12]}.")
    parser.add_argument("--dest", type=Path, required=True, help="directory to put the model in")
    args = parser.parse_args(argv)
    try:
        fetched = fetch(args.dest)
    except ModelFileError as error:
        sys.exit(f"fetch_model: {error}")
    print(f"fetch_model: {args.dest} is complete ({len(fetched)} files fetched)")


if __name__ == "__main__":
    main()
