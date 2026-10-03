"""The third-party notices: each font family has its licence text beside its files, the notice
names what the repository ships, the README links it, and the image carries the licences."""

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


def test_every_font_file_is_listed_and_the_open_question_is_stated() -> None:
    for family in FAMILIES:
        for font in (FONTS / family).glob("*.woff2"):
            assert font.parent.name in NOTICES
    assert "Reserved Font Name" in NOTICES
    assert "byte-identical" in NOTICES
    assert "@fontsource" in NOTICES


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
