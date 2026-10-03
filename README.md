# AI Gateway

A zero-trust gateway that sits between AI clients and the MCP tools they call, enforcing authentication, per-tool authorization, and layered prompt-injection defense on every request. This is a portfolio project: the demo company it serves, Harborline Supply Co., is fictional, and all of its data (customers, orders, inventory, documents) is synthetic.

## Quick start

```sh
python3 scripts/init_env.py            # writes .env with fresh random secrets (0600)
docker compose up -d --build --wait    # Postgres, migrations, gateway, ticketing, CRM, handbook
```

`init_env.py` replaces every `change-me-...` placeholder in `.env.example` with a random
secret. If a `.env` already exists it only appends the keys the example has and the file
lacks (so pulling a release with a new server needs no `--force`), never changes a line you
have, and a second run changes nothing; `--force` regenerates everything. The MCP servers
refuse to start with a service credential shorter than 32 characters or a `change-me`
placeholder, so this step is not optional. The first build downloads the 30 MB embedding
model of the handbook server (checked against pinned hashes); after that nothing needs the
network. See `docs/architecture.md` for the rest.

### Simulated traffic and telemetry

The gateway stores a record of every request, its spans and every failed authentication in
Postgres (see Telemetry in `docs/architecture.md`). To fill it with a repeatable, seeded mix of
normal calls, refused calls and failed logins as both fictional bots, with no API key:

```sh
docker compose run --rm -T admin seed-demo > demo.json    # tokens; keep it out of git
uv run scripts/simulate_traffic.py --tokens-file demo.json --calls 300 --verify
rm demo.json
```

`--verify` reads the stored telemetry back through the dashboard's read-only role and checks it
matches what was sent. Records older than 30 days (spans: 7) are purged hourly.

## Licence

MIT, copyright AOX LLC. See `LICENSE`. Harborline Supply Co. and all its data are fictional.
