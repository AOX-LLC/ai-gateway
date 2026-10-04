"""The third-party notices: each font family has its licence text beside its files, the notice
names what the repository ships, the README links it, and the image carries the licences."""

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FONTS = ROOT / "dashboard" / "src" / "fonts"
FAMILIES = ["ibm-plex-sans", "ibm-plex-mono", "space-grotesk"]
NOTICES = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("family", FAMILIES)
def test_each_font_family_has_its_licence_beside_its_files(family: str) -> None:
    folder = FONTS / family

    assert list(folder.glob("*.woff2")), "font files"
    licence = (folder / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in licence
    assert "Copyright" in licence.splitlines()[0]
    assert f"dashboard/src/fonts/{family}/OFL.txt" in NOTICES


def test_every_font_file_is_listed_with_its_hash_and_is_ibms_own_file() -> None:
    counts = {family: len(list((FONTS / family).glob("*.woff2"))) for family in FAMILIES}
    assert counts == {"ibm-plex-sans": 4, "ibm-plex-mono": 2, "space-grotesk": 2}, (
        "a font file was added or removed: update THIRD_PARTY_NOTICES.md and this count"
    )
    for family in FAMILIES:
        assert f"dashboard/src/fonts/{family}/*.woff2" in NOTICES
    for font in (FONTS / "ibm-plex-sans").glob("*.woff2"):
        assert re.fullmatch(r"IBMPlexSans-[A-Za-z]+-Latin1\.woff2", font.name), "IBM's own name"
    for font in (FONTS / "ibm-plex-mono").glob("*.woff2"):
        assert re.fullmatch(r"IBMPlexMono-[A-Za-z]+-Latin1\.woff2", font.name), "IBM's own name"
    for family in ("ibm-plex-sans", "ibm-plex-mono"):
        for path in (FONTS / family).glob("*.woff2"):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert f"`{path.name}` | `{digest}`" in NOTICES, f"{path.name}: hash in the notice"
    assert "@ibm/plex-sans" in NOTICES
    assert "open question" not in NOTICES.lower()
    assert "@fontsource" not in NOTICES.split("Space Grotesk files are", 1)[0], (
        "the Plex files no longer come from Fontsource"
    )


def test_the_plex_licence_texts_are_ibms_own_with_the_reserved_font_name() -> None:
    for family in ("ibm-plex-sans", "ibm-plex-mono"):
        first = (FONTS / family / "OFL.txt").read_text(encoding="utf-8").splitlines()[0]
        assert 'Reserved Font Name "Plex"' in first


def test_the_dashboard_loads_only_the_font_files_that_exist() -> None:
    source = (ROOT / "dashboard" / "src" / "app" / "fonts.ts").read_text(encoding="utf-8")
    named = re.findall(r'path: "\.\./fonts/([^"]+)"', source)
    assert named
    for relative in named:
        assert (FONTS / relative).is_file(), relative


def test_the_notice_covers_the_database_image_by_digest_and_the_icon_set() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    image = next(
        line.split("image:")[1].strip()
        for line in compose.splitlines()
        if "pgvector/pgvector" in line
    )

    assert image in NOTICES, "the image the stack pulls, with its digest"
    assert "PostgreSQL License" in NOTICES
    assert "Tabler Icons" in NOTICES
    assert "MIT License" in NOTICES


def test_the_readme_links_the_notices_from_its_licence_section() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    licence = readme.split("## Licence", 1)[1]

    assert "(THIRD_PARTY_NOTICES.md)" in licence


def test_the_dashboard_image_carries_the_font_licences_and_leaves_out_the_lgpl_image_library() -> (
    None
):
    dockerfile = (ROOT / "dashboard" / "Dockerfile").read_text(encoding="utf-8")
    config = (ROOT / "dashboard" / "next.config.ts").read_text(encoding="utf-8")

    for family in FAMILIES:
        assert f"/app/src/fonts/{family}/OFL.txt" in dockerfile
    assert "outputFileTracingExcludes" in config
    assert "node_modules/sharp" in config
    assert "node_modules/@img" in config


def test_the_image_carries_every_licence_text_the_notice_promises() -> None:
    dockerfile = (ROOT / "dashboard" / "Dockerfile").read_text(encoding="utf-8")

    for family in FAMILIES:
        assert f"/fonts/{family}/OFL.txt" in dockerfile
    assert "licenses/tabler-icons-MIT.txt" in dockerfile
    assert (
        (ROOT / "dashboard" / "licenses" / "tabler-icons-MIT.txt")
        .read_text(encoding="utf-8")
        .startswith("MIT License")
    )
