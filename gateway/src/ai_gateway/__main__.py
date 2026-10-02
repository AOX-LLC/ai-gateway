"""Run the gateway: `ai-gateway`, or `python -m ai_gateway`."""

import logging

import uvicorn

from ai_gateway.app import create_app
from ai_gateway.settings import GatewaySettings


def main() -> None:
    settings = GatewaySettings()  # database_url comes from the environment
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(
        create_app(settings),
        host=settings.bind,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # Never trust X-Forwarded-* headers: nothing sits in front of the gateway yet.
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
