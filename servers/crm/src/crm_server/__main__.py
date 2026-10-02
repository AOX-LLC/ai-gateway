"""Run the CRM server with uvicorn (CRM_* environment variables)."""

import logging
import sys

import uvicorn

from crm_server.server import build_app
from crm_server.settings import CrmSettings
from mcp_common.credentials import MissingCredentialError, WeakCredentialError


def main() -> None:
    settings = CrmSettings()
    logging.basicConfig(level=settings.log_level)
    try:
        app = build_app(settings)
    except (MissingCredentialError, WeakCredentialError) as error:
        sys.exit(f"crm-server: {error}")
    uvicorn.run(app, host=settings.bind, port=settings.port)


if __name__ == "__main__":
    main()
