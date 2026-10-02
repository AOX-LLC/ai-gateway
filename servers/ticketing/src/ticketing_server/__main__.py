"""Run the ticketing server with uvicorn (TICKETING_* environment variables)."""

from mcp_common.server import run_server
from ticketing_server.server import build_app
from ticketing_server.settings import TicketingSettings


def main() -> None:
    run_server("ticketing-server", TicketingSettings, build_app)


if __name__ == "__main__":
    main()
