"""Run the one-shot setup.

HARBORLINE_SETUP_DATABASE_URL   the OWNER connection string
TICKETING_DB_PASSWORD           the password to give the ticketing_app role
HARBORLINE_TICKETING_EXTRA_RECORDS  optional JSON file of extra tickets and comments
"""

import logging
import os
import sys
from pathlib import Path

import anyio

from harborline_setup.ticketing import setup_ticketing


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    owner_url = os.environ.get("HARBORLINE_SETUP_DATABASE_URL", "")
    password = os.environ.get("TICKETING_DB_PASSWORD", "")
    if not owner_url or not password:
        sys.exit("harborline-setup: set HARBORLINE_SETUP_DATABASE_URL and TICKETING_DB_PASSWORD")
    extra = os.environ.get("HARBORLINE_TICKETING_EXTRA_RECORDS")
    anyio.run(setup_ticketing, owner_url, password, Path(extra) if extra else None)
    print("harborline-setup: done")


if __name__ == "__main__":
    main()
