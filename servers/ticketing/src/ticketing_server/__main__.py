"""Run the ticketing server with uvicorn (TICKETING_* environment variables)."""

import logging
import sys

import uvicorn

from mcp_common.credentials import MissingCredentialError, WeakCredentialError
from ticketing_server.server import build_app
from ticketing_server.settings import TicketingSettings


def main() -> None:
    settings = TicketingSettings()
    logging.basicConfig(level=settings.log_level)
    try:
        app = build_app(settings)
    except (MissingCredentialError, WeakCredentialError) as error:
        sys.exit(f"ticketing-server: {error}")
    uvicorn.run(app, host=settings.bind, port=settings.port)


if __name__ == "__main__":
    main()
