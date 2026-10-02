"""Run the one-shot setup.

HARBORLINE_SETUP_DATABASE_URL       the OWNER connection string
TICKETING_DB_PASSWORD               the password to give the ticketing_app role
CRM_DB_PASSWORD                     the password to give the crm_app role
HARBORLINE_TICKETING_EXTRA_RECORDS  optional JSON file of extra tickets and comments
HARBORLINE_CRM_EXTRA_RECORDS        optional JSON file of extra accounts and notes
"""

import logging
import os
import sys
from pathlib import Path

import anyio

from harborline_setup.crm import setup_crm
from harborline_setup.ticketing import setup_ticketing

_REQUIRED = ["HARBORLINE_SETUP_DATABASE_URL", "TICKETING_DB_PASSWORD", "CRM_DB_PASSWORD"]


def _optional_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


async def _run() -> None:
    owner_url = os.environ["HARBORLINE_SETUP_DATABASE_URL"]
    await setup_ticketing(
        owner_url,
        os.environ["TICKETING_DB_PASSWORD"],
        _optional_path("HARBORLINE_TICKETING_EXTRA_RECORDS"),
    )
    await setup_crm(
        owner_url, os.environ["CRM_DB_PASSWORD"], _optional_path("HARBORLINE_CRM_EXTRA_RECORDS")
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        sys.exit(f"harborline-setup: set {' and '.join(missing)}")
    anyio.run(_run)
    print("harborline-setup: done")


if __name__ == "__main__":
    main()
