"""Run the CRM server with uvicorn (CRM_* environment variables)."""

from crm_server.server import build_app
from crm_server.settings import CrmSettings
from mcp_common.server import run_server


def main() -> None:
    run_server("crm-server", CrmSettings, build_app)


if __name__ == "__main__":
    main()
