"""Run the handbook server with uvicorn (HANDBOOK_* environment variables)."""

import logging
import sys

import uvicorn

from handbook_server.embedding import ModelNotFoundError
from handbook_server.server import build_app
from handbook_server.settings import HandbookSettings
from mcp_common.credentials import MissingCredentialError, WeakCredentialError


def main() -> None:
    settings = HandbookSettings()
    logging.basicConfig(level=settings.log_level)
    try:
        app = build_app(settings)
    except (MissingCredentialError, WeakCredentialError, ModelNotFoundError) as error:
        sys.exit(f"handbook-server: {error}")
    uvicorn.run(app, host=settings.bind, port=settings.port)


if __name__ == "__main__":
    main()
