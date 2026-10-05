# Contributing to AI Gateway

These conventions apply to every contributor, human or AI.

## Layout

- The main checkout stays on `main` and is never edited directly.
- All work happens in a worktree at `.worktrees/<branch>`. `.worktrees/` is gitignored.
- Local-only, gitignored files (such as `.env` and local notes) live in the main checkout and are copied into each new worktree, because merging removes the worktree.
- Branches are named `phase-N-<slug>`, for example `phase-1-gateway-skeleton`.
- Cut every branch from an up-to-date `origin/main`:

  ```sh
  git fetch origin
  git worktree add .worktrees/phase-N-<slug> -b phase-N-<slug> origin/main
  ```

## Ports

Every service that is published is published on `127.0.0.1` only, never `0.0.0.0`. Inside a container a process has to listen on `0.0.0.0` for Docker to publish its port, so the host side of every Compose port mapping is `127.0.0.1`. Outside Docker, services bind `127.0.0.1`.

| Service | Port |
| --- | --- |
| Dashboard | 4400 |
| Gateway | 4401 |
| PostgreSQL | 4402 |
| MCP servers | 4410–4412, inside the Compose network only; no host port |
| Lab upstream (`lab` profile only) | 4413, inside the Compose network only; no host port |

The MCP servers sit on an internal Compose network (`backend`) with no route to the outside world, so they publish nothing on the host, and the host has no address on that network, so they cannot reach the host's own services either. Reach them from inside the network: `scripts/direct_check.sh` runs the direct scenarios there, and `scripts/check_servers_have_no_internet.sh` proves they cannot leave. Only the gateway and PostgreSQL are on both the internal network and `edge`.

The Docker Compose project name is `ai-gateway`.

## Data

- Use synthetic or openly licensed data only. Never use real customer, company, or personal data.
- Harborline Supply Co. is fictional. Label it as fictional wherever it appears: seed data, the dashboard, docs, and screenshots.

## Secrets

- Commit `.env.example` with placeholder values only. Real `.env` files are gitignored.
- A gitleaks pre-commit hook scans every commit. Do not bypass it.
- Never commit real keys, tokens, or credentials. If one is committed, rotate it; removing it from history is not enough.

## Commits

- One concern per commit. Refactors and behavior changes go in separate commits.
- Subject lines are in the imperative mood: "Add tool allowlist", not "Added tool allowlist".
- No attribution or co-author trailers in commit messages or PR descriptions.

## Stack

- Gateway and MCP servers: Python 3.12 (pinned in `.python-version`, and the version of every image), FastAPI, Pydantic, and the official MCP Python SDK.
- Audit log and approvals: agent-core, pinned by git tag in `gateway/pyproject.toml`.
- Database: PostgreSQL with pgvector.
- Dashboard: Next.js and TypeScript.
- Orchestration: Docker Compose.
- CI: GitHub Actions.

## Runnable in five minutes

- `docker compose up` brings up the whole stack with seeded fictional data.
- A mock mode runs the full stack with no API key. Keep it working: any feature that calls a model provider needs a mock path.
