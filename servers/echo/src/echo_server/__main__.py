"""Run the echo test fixture with uvicorn (ECHO_HOST, ECHO_PORT, ECHO_ALLOWED_HOSTS)."""

import os

import uvicorn

from echo_server.server import build_app


def main() -> None:
    allowed_hosts = os.environ.get("ECHO_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*").split(",")
    uvicorn.run(
        build_app([host.strip() for host in allowed_hosts if host.strip()]),
        host=os.environ.get("ECHO_HOST", "127.0.0.1"),
        port=int(os.environ.get("ECHO_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
