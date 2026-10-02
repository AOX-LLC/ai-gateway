"""Run the handbook server with uvicorn (HANDBOOK_* environment variables)."""

from handbook_server.embedding import ModelNotFoundError
from handbook_server.server import build_app
from handbook_server.settings import HandbookSettings
from mcp_common.server import run_server


def main() -> None:
    run_server("handbook-server", HandbookSettings, build_app, (ModelNotFoundError,))


if __name__ == "__main__":
    main()
