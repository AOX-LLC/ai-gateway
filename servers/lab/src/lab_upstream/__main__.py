"""Run the lab upstream with uvicorn (LAB_* environment variables)."""

import sys

import uvicorn

from lab_upstream.server import LabNotEnabledError, build_app
from lab_upstream.settings import LabSettings
from mcp_common.credentials import MissingCredentialError, WeakCredentialError


def main() -> None:
    settings = LabSettings()
    try:
        app = build_app(settings)
    except (LabNotEnabledError, MissingCredentialError, WeakCredentialError, ValueError) as error:
        sys.exit(f"lab-upstream: {error}")
    uvicorn.run(app, host=settings.bind, port=settings.port)


if __name__ == "__main__":
    main()
