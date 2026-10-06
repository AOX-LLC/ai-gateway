#!/usr/bin/env python3
"""Plant, or remove, the injected ticket of the real-client clip on the demo stack.

    uv run demo/real-client/plant.py plant      # prints the planted ticket's id
    uv run demo/real-client/plant.py clean <id>
    uv run demo/real-client/plant.py count      # tickets in the stack, to show none was added

The ticket is the export attack's own (scripts/redteam/attacks/export-every-customer.toml): a
customer's message that tells an assistant to export every customer, inserted the way a message
would arrive. Its text is the classifier corpus's `email-injection` string, so replay mode has a
recording for it. Fictional data only. The database owner's login comes from .env and is never
printed; this talks to 127.0.0.1:4402 only.
"""

import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

# The red-team oracle's own plant and clean-up, so the clip plants what the attack plants.
from redteam import oracle  # noqa: E402

ACCOUNT_ID = "ACC-00003"
SUBJECT = "Order question"
CORPUS_ID = "email-injection"
MARKER = "RT-export"
DATABASE_PORT = 4402
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.startswith("#"):
            values[key.strip()] = value.strip()
    return values


def owner_url() -> str:
    env = env_file()
    user, password = quote(env["POSTGRES_USER"], safe=""), quote(env["POSTGRES_PASSWORD"], safe="")
    return f"postgresql://{user}:{password}@127.0.0.1:{DATABASE_PORT}/{env['POSTGRES_DB']}"


def injected_text() -> str:
    path = ROOT / "config" / "classifier_corpus" / "story_09.toml"
    corpus = tomllib.loads(path.read_text("utf-8"))
    return next(item["text"] for item in corpus["item"] if item["id"] == CORPUS_ID)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "plant":
        ticket_id, _ = oracle.plant_ticket(owner_url(), ACCOUNT_ID, SUBJECT, injected_text())
        print(ticket_id)
    elif command == "clean" and len(sys.argv) == 3:
        # The planted ticket and any ticket that carries the attack's marker.
        oracle.clean_up(owner_url(), EPOCH, sys.argv[2], MARKER)
    elif command == "count":
        with oracle.psycopg.connect(owner_url()) as connection:
            row = connection.execute("SELECT count(*) FROM ticketing.tickets").fetchone()
        print(row[0] if row else 0)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
