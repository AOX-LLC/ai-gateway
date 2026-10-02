# AI Gateway

A zero-trust gateway that sits between AI clients and the MCP tools they call, enforcing authentication, per-tool authorization, and layered prompt-injection defense on every request. This is a portfolio project: the demo company it serves, Harborline Supply Co., is fictional, and all of its data (customers, orders, inventory, documents) is synthetic.

## Quick start

```sh
python3 scripts/init_env.py            # writes .env with fresh random secrets (0600)
docker compose up -d --build --wait    # Postgres, migrations, gateway, ticketing server
```

`init_env.py` replaces every `change-me-...` placeholder in `.env.example` with a random
secret, and refuses to overwrite an existing `.env` unless you pass `--force`. The MCP servers
refuse to start with a service credential shorter than 32 characters or a `change-me`
placeholder, so this step is not optional. See `docs/architecture.md` for the rest.
