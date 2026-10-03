"""The Python the tests run on is the Python the images run on."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_tests_run_on_the_python_the_images_are_built_on() -> None:
    image_versions = set()
    for dockerfile in ROOT.glob("**/Dockerfile"):
        if {".worktrees", ".venv"} & set(dockerfile.relative_to(ROOT).parts):
            continue
        image_versions |= set(re.findall(r"^FROM python:(\d+\.\d+)-", dockerfile.read_text(), re.M))

    assert image_versions == {"3.12"}, "every image should build on one Python"
    assert f"{sys.version_info.major}.{sys.version_info.minor}" in image_versions


def test_the_repository_pins_the_python_the_images_use() -> None:
    assert (ROOT / ".python-version").read_text().strip() == "3.12"
