# Architecture

AI Gateway sits between AI clients and the internal tools of a company, exposed over the
Model Context Protocol (MCP). The company in this repository, Harborline Supply Co., is
fictional, and all of its data is synthetic.

Every request is authenticated, then passes through one ordered chain of checks. Each
check can be set to enforce, monitor or off through configuration, with no code change.
That is what makes the project's headline result possible: a red-team scorecard showing
the attack success rate with each defense layer on and off.

This document describes Phase 1, the skeleton, Phase 2a, the first MCP server behind it, and
the seams later phases build on.

## At a glance

| Topic | Decision |
| --- | --- |
| MCP revision | Targets **2025-11-25**, the newest revision with the initialize handshake and sessions. Older handshake revisions are negotiated by the SDK. Requests that declare the stateless 2026-07-28 revision get a clear 400; current clients then fall back to the handshake. |
| MCP SDK | The official Python SDK, pinned to `mcp==2.2.0` |
| Tool names | `<namespace>__<tool>`, e.g. `echo__say` |
| Authentication | Bearer tokens, stored as SHA-256 hashes and compared in constant time. It runs before the chain, and no configuration can turn it off. |
| Authorization | Tool-level scopes per client, enforced by the `scope` layer |
| Data store | PostgreSQL, accessed through psycopg 3 and plain SQL migrations |
| MCP servers | Low-level `mcp.server.Server` with strict tool schemas, one Postgres schema and role each, a service credential in front. See [MCP servers](#mcp-servers-phase-2a). |
| Tool effects | `read` or `write`, from a reviewed policy table. A tool with no policy is a write. |

## Request flow

```
AI client ──HTTPS──► bearer auth ──► protocol version guard ──► MCP endpoint /mcp
                    (always on)                                      │
                                         resolve tool name in the cached catalog
                                         (unknown: same answer as out of scope)
                                                                     ▼
              ┌──────────────── pipeline, one run per request ────────────────┐
  tools/list  │ filter_tools:  scope → allowlist → pinned descriptions …      │
  tools/call  │ before_call:   scope → allowlist → rate limit → schema →      │
              │                egress → canary → classifier → approval        │
              │ ── forward exactly the checked arguments to the upstream ──   │
              │ after_call:    egress → canary → classifier (on results)      │
              └───────────────────────────────────────────────────────────────┘
                 every layer's verdict → one decision record → event sink + span
```

Phase 1 implements `scope`. Phase 3 adds `allowlist`, `rate_limit` and `approval`. Phase 4
adds `schema`, `pinned_descriptions`, `egress`, `canary` and `classifier`.

## The pipeline

A layer subclasses `BaseLayer` (`gateway/src/ai_gateway/pipeline/types.py`) and overrides the
hooks it needs:

- `filter_tools` may remove tools from a tools/list answer, and nothing else.
- `before_call` returns `Allow` or `Deny` before the upstream is called.
- `after_call` returns `Allow` or `Deny` after the upstream answers.

Layers return verdicts and never call upstream. The runner (`pipeline/runner.py`) owns the
upstream call, so no layer can skip the layers after it or call a tool twice.

### Modes

| Mode | Effect |
| --- | --- |
| `enforce` | A Deny stops the request. |
| `monitor` | A Deny is recorded as `would_block` and the request continues. This measures detection apart from prevention. |
| `off` | The layer does not run, and is recorded as `off`. |

### Rules

- **Fixed order.** Code fixes the order (`pipeline/registry.py`). Configuration can switch
  layers on and off but cannot reorder them, because the order is itself a security
  property: approval runs last, so a person approves exactly the call that runs.
- **Fail closed.** A layer that raises an exception blocks the request in enforce mode and
  is recorded as `error` in monitor mode.
- **Immutable arguments.** A `ToolCall` holds its arguments as canonical JSON, so no layer
  can change them in place. The arguments forwarded upstream are byte-identical to the
  ones every layer saw, and their SHA-256 is recorded.
- **One decision record per request.** It holds each layer's mode, verdict, code and
  timing, the outcome, the upstream status, the argument hash, the tool's `effect` and
  `effect_source`, the negotiated protocol version and a fingerprint of the pipeline
  configuration. It never holds arguments,
  results or credentials. The scorecard is computed from these records, not from the
  errors clients see.

### Configuration

The configuration lives in `config/pipeline.toml`, whose path is set by
`GATEWAY_PIPELINE_FILE`. It is read once at startup:

```toml
[layers]
scope = "enforce"        # enforce | monitor | off

[safety]
allow_floor_override = false
```

- **Mistakes stop startup.** An unknown layer, mode or key stops the gateway from starting.
- **Fail safe.** A layer the file does not mention runs in `enforce`.
- **Floor layers are guarded.** `scope` is a floor layer: setting it to monitor or off
  needs `allow_floor_override = true` and logs a warning.

To produce the scorecard, the red-team harness writes one file per column and restarts the
gateway between runs. There is no runtime or per-request switch for an attacker to flip.

### Errors clients see

- **Unknown or out-of-scope tool:** JSON-RPC `-32602`, `Tool 'x' is not available to this
  client.` It is the same text in both cases, so it cannot be used to discover tools.
- **Blocked by any other layer:** `-32010`, `Request blocked by gateway policy.`, with a
  request id. The layer name appears only in the decision record.
- **Upstream failure:** a tool result with `isError: true`, naming the service and the
  request id, so the model can react.
- **Unexpected exceptions:** caught at the handler and returned as a generic internal error
  with a request id. Exception text never reaches the client.

## Authentication

Tokens look like `aig_<lookup id>_<secret>`:

- The lookup id is 8 base32 characters. It is not secret; it finds the row.
- The secret is 256 random bits.
- Only the SHA-256 of the whole token is stored. A slow password hash would add latency
  to every request and no safety, because a 256-bit random value cannot be guessed
  offline.
- A gitleaks rule in `.gitleaks.toml` blocks any committed token.

Verification (`auth/verifier.py`) runs on every HTTP request:

1. Parse the token. A malformed token fails like any other.
2. Look up the row by lookup id, with the client's status and scopes, in one query.
   Nothing is cached, so revocation and scope changes apply on the next request.
3. Compare hashes with `hmac.compare_digest`. When no row matches, it compares against a
   dummy hash, so an unknown lookup id costs the same as a wrong secret.
4. Reject revoked or expired tokens and disabled clients.

Every failure gets the same `401` body. A missing token gets
`WWW-Authenticate: Bearer realm="ai-gateway"`, and any presented token that fails adds
`error="invalid_token"`, following RFC 6750. Each failure emits a `gateway.auth_failure`
event with the reason, the lookup id if the token was well formed, and the peer address.
Phase 3's rate limiting and IP banning will consume these events.

The peer address is the direct TCP peer. Behind Docker's port publishing it is likely the
bridge gateway for every host client, so IP bans must not key on it until real client
addresses reach the gateway, through a trusted proxy header set by whatever sits in front.

### Keeping the raw token out of memory and logs

- **Request context:** the SDK's `AccessToken` carries the lookup id, never the raw token.
- **Logs:** no code path logs headers, and tests assert that no token secret appears in
  logs or events.
- **Sessions:** the SDK binds an MCP session to the client that opened it. Replaying
  another client's session id gets a 404.

### Rotation

Rotation uses `gateway-admin token-rotate --client <name> --grace-hours 24`. It issues a new
token and brings the expiry of the client's older tokens forward to the end of the grace
period. A client can hold at most two live tokens. `token-revoke` takes effect on the next
request.

## Upstream servers

### Catalog (`proxy/catalog.py`)

- **Cached answers.** tools/list is answered from a cache and never waits on an upstream.
- **Refresh.** Each upstream has its own background task, which refreshes it on a
  short-lived connection under that upstream's own timeout: every 60 s while it is
  healthy, and with exponential backoff from 1 s to 60 s while it is not. A hung upstream
  delays only itself.
- **New upstreams.** The registry is polled every 5 s, so a newly registered upstream
  is picked up within seconds.
- **Failed refresh.** The upstream's tools disappear until it answers again, so clients
  never see stale descriptions.
- **Name checks.** Tool names that Claude clients cannot accept (`[A-Za-z0-9_-]{1,64}`
  after namespacing) are dropped with a warning, and so are duplicates.

### Service credentials

An upstream row may name an environment variable in `credential_env`. The gateway reads
that variable when it connects and sends `Authorization: Bearer <value>` on every
upstream connection, both the catalog refresh and the per-session call connections. The
registry holds the variable's *name*, never the value, and the value is never logged.

If the variable is missing or empty, that upstream is unavailable and one clear warning
names the variable. The gateway never falls back to calling a protected server without
its credential.

### Calls (`proxy/sessions.py`)

- **One connection set per session.** Each downstream MCP session gets its own connection
  to each upstream it calls, opened on first use. No upstream session state, cancellation
  or server-initiated message can cross between clients.
- **Cleanup.** Connections close when the client ends its session, after 15 minutes idle,
  or at shutdown.

### Timeouts and failures

| Event | Result |
| --- | --- |
| Connecting takes longer than 5 s, or the call takes longer than its per-upstream timeout (30 s by default) | Tool error, and the upstream request is cancelled |
| The client goes away mid-call | The upstream call is cancelled |
| A connection drops | The connection is replaced on the next call |

A call that may have reached an upstream is never retried, because later phases add tools
that write.

## MCP servers (Phase 2a)

Harborline Supply Co. is fictional, and so is everything its servers hold. Phase 2 is split:

| Part | Holds |
| --- | --- |
| **2a** (this document) | The shared foundation (`mcp-common`), the gateway changes below, and the **ticketing** server on port 4412, namespace `tickets` |
| **2b** (later) | The CRM server (4410, shares the fictional accounts `ACC-00001` to `ACC-00040`) and the handbook server (4411) |

### Shared foundation (`servers/common`, module `mcp_common`)

- **Strict tools (`toolset.py`).** The SDK's high-level `MCPServer` (2.2.0) silently drops
  arguments a tool did not declare and omits `additionalProperties: false`, so servers use
  the low-level `Server` and a `StrictToolset`. Every input is a Pydantic model with
  `extra="forbid"`. A call with an unknown tool, an extra argument or an invalid value is a
  `-32602` error with a short message that never echoes the arguments. A handler's own
  failure (an unknown ticket, say) is a tool error result, so the model can react.
- **Schema contract (`schema_contract.py`).** Tests walk each tool's `inputSchema`, and the
  gateway catalog's copy of it, and fail on a missing `additionalProperties: false`, a
  string with no `maxLength`, `enum` or `pattern`, an unbounded integer, an array with no
  `maxItems`, or a nested object.
- **Service credential (`credentials.py`).** The MCP route requires
  `Authorization: Bearer <credential>`, compared with `hmac.compare_digest`. Every failure
  is the same bare `401`. A server with a missing or empty credential refuses to start.
  `/healthz` is open (it discloses nothing secret). The ticketing server answers it in the
  gateway's identity format, below; a server that passes no responder says only `ok`.
- **Migrations (`migrate.py`).** `apply_migrations(conninfo, package, schema)` serves the
  gateway's registry (`public`) and each server's own schema. Every schema keeps its own
  `schema_migrations` table and advisory lock.

### The ticketing server

Tools, in the gateway namespace `tickets`:

| Tool | Effect | Does |
| --- | --- | --- |
| `list_tickets(status?, priority?, account_id?, limit)` | read | Up to 20 ticket summaries, newest activity first |
| `get_ticket(ticket_id)` | read | One ticket and its newest **public** comments (at most 20, plus `comments_total`) |
| `create_ticket(subject, description, priority, account_id)` | write | Opens a ticket, returns its id |
| `add_comment(ticket_id, body)` | write | Adds a public comment (always public) |
| `change_status(ticket_id, status)` | write | Sets the status |
| `assign(ticket_id, assignee)` | write | Assigns a staff handle |

- **Allowlisted output.** Results are built from explicit column lists and output models.
  `internal_notes` and internal comments are never selected and have no field to land in.
  A test scans every read tool's output for every seeded internal value.
- **Seed.** Deterministic (`random.Random(20261002)`, a fixed base time, fixed word lists):
  12 staff, 80 tickets, 250 comments. See `servers/ticketing/data/README.md`.

### Schema and role isolation

Each server owns one Postgres schema and one role, and no role can see another's data:

| Role | Schema | Rights |
| --- | --- | --- |
| `gateway_app` | `public` (the registry) | read, and update `client_tokens.last_used_at` |
| `ticketing_app` | `ticketing` | read all tables; insert tickets and comments; update only `tickets.status`, `assignee` and `updated_at`; use the two sequences. No delete, truncate or create. |

`REVOKE ALL ON SCHEMA ticketing FROM PUBLIC` keeps every other role out, and `ticketing_app`
has nothing in `public`. Tests assert each of these, including the column-level updates.

The `harborline-setup` one-shot (the `servers-setup` Compose service) runs as the database
owner and is safe to repeat. It creates the role if missing, then `ALTER ROLE ... PASSWORD`
with a quoted literal (so it also rotates the password), creates the schema, runs the
schema's migrations, **applies the role's grants on every run** (not in a migration, so they
never depend on when the role was created) and seeds the schema when empty. A role cannot only
live in `db/init`, which runs once on a fresh volume, so existing volumes get it from this
step. The ticketing process itself connects only as `ticketing_app`.

### Tool effects: read or write, fail closed

Whether a call can change anything is the gateway's decision, never the upstream's. The
`tool_policies` table holds a reviewed `read` or `write` per tool, loaded from
`config/tool_policies.toml` (`gateway-admin seed-demo`) or set one at a time
(`gateway-admin tool-policy-set tickets__get_ticket read`).

- A tool with a policy row has that effect, with `effect_source: "policy"`.
- A tool with no row is a **write**, with `effect_source: "default"`. Adding a tool to a
  server therefore never makes it look harmless.
- The upstream's `readOnlyHint` is ignored. If it contradicts the policy, the gateway logs
  one warning (a drift signal) and uses the policy. The hint clients see in `tools/list` is
  the gateway's own verdict.
- Policies are re-read every registry poll (5 s), so a change applies within seconds.
- Every decision record carries `effect` and `effect_source`. Phase 3's approval layer keys
  on the effect.

### Client identity in `_meta`: attribution, not authorization

On every forwarded `tools/call` the gateway sends only its own `_meta`:
`{"io.aox.ai-gateway/client": "<authenticated client name>"}`. Whatever `_meta` the client
sent, including that same key, is dropped and never forwarded, so a client cannot claim to
be another one. A server may record the name, for example as a ticket's `requested_by`, and
uses `direct` when the key is absent or not a valid client name. **It is attribution only.**
Anything that can reach a server directly can write any value there, so a server must never
use it to decide what a caller may do. Authorization is the gateway's scope check.

### Rule for 2b

> Handbook search must exclude restricted documents inside the SQL query (WHERE clause),
> before ranking and LIMIT — never by filtering results afterwards.

Filtering afterwards leaks through rank order, counts and truncated result sets, and it
puts restricted text in server memory.

## Seams for later phases

The seams that the shared `agent-core` library will fill (approval queue, audit log,
tracing) follow its **pre-release, unmerged interfaces**. Those may shift before its v0.1.0
release, so expect small adapter changes when it lands. This repository does not import
it or copy its code.

| Seam | Phase 1 | Later |
| --- | --- | --- |
| `EventSink` (`seams/events.py`) | Writes JSON lines to the log. Events already follow the audit log's record shape: dotted action, actor id `client:<uuid>`, subject, a small payload with no secret-named keys. | Phase 3 appends them to agent-core's hash-chained audit log. |
| `ApprovalGate` (`seams/approvals.py`) | Protocol only | Phase 3's `approval` layer submits the call, waits for a person, and checks the approval matches the exact argument hash. |
| Tracing | OpenTelemetry API only, so it records nothing until an SDK is configured. Span names are in `telemetry/attributes.py`. | Phase 5 configures a self-hosted exporter for the dashboard. |

## Data model

| Table | Holds |
| --- | --- |
| `clients` | name, description, status (`active` or `disabled`) |
| `client_tokens` | lookup id, SHA-256, label, created, expires, revoked, last used |
| `client_scopes` | one row per granted exposed tool name |
| `upstream_servers` | namespace, URL, timeouts, the *name* of an environment variable holding its credential (never the value) |
| `tool_policies` | namespace, upstream tool name, `effect` (`read` or `write`), notes, when it was reviewed |

### Database roles

- **Owner:** the admin CLI and migrations run as the owner, from a separate `admin` Compose
  service.
- **`gateway_app`:** the gateway itself connects as `gateway_app`. It may read the
  registry and update `client_tokens.last_used_at`, and nothing else. It has no access to
  any server's schema.
- **`ticketing_app`:** the ticketing server connects as `ticketing_app`, described under
  [MCP servers](#mcp-servers-phase-2a). It has no access to the registry.

## Reaching the gateway from another machine

The gateway listens on 127.0.0.1 only. For a demo with a desktop AI client on another
machine, these options keep that binding. None of them is built into this repository.

| Option | How | When |
| --- | --- | --- |
| SSH local forward | `ssh -N -L 4401:127.0.0.1:4401 <gateway host>`; the client uses `http://127.0.0.1:4401/mcp` | The simplest option. Encrypted, nothing exposed. A desktop app that only launches local stdio servers can use a stdio-to-HTTP MCP bridge that adds the Authorization header. |
| Private mesh VPN with a local HTTPS proxy | For example, Tailscale Serve in front of `127.0.0.1:4401` | Several of your own devices. Add the mesh host name to `GATEWAY_ALLOWED_HOSTS`. |
| Tunnel with an access proxy | For example, Cloudflare Tunnel with Cloudflare Access in front, bearer auth still enforced | Clients whose remote connectors run in a vendor's cloud. Shut it down after the demo. |
| Reverse SSH tunnel | The gateway host runs `ssh -R` to the client machine | The client machine cannot reach the gateway host directly. |

Any option that crosses a network carries the bearer token, so it must use TLS or SSH.

## Health check

`GET /healthz` needs no token and, like everything else, is published on 127.0.0.1 only.
The gateway answers 200 while the MCP endpoint is serving and 503 otherwise. The ticketing
server (4412) uses the same format and the same cache (`mcp_common.health`) and answers 200
while it can read its own schema version, 503 with `status: "unavailable"` when it cannot.
Its `GIT_COMMIT`/`GIT_BRANCH` come from the build arguments of `servers/Dockerfile`, its
`version` is the `ticketing-server` package, and `schema_version` is the newest migration in
the `ticketing` schema (its role may read `ticketing.schema_migrations`, nothing more there).

| Field | Meaning |
| --- | --- |
| `status` | `ok` or `unavailable` |
| `commit` | The commit the image was built from, set by the `GIT_COMMIT` build argument; `null` when not given |
| `commit_source` | `process_start`: the commit is fixed for the life of the process |
| `branch` | Set by the `GIT_BRANCH` build argument; `null` when not given |
| `version` | The `ai-gateway` package version |
| `schema_version` | The newest applied migration of the registry, zero-padded (`"0003"`). Re-read at most every 30 s by one caller at a time, with the read bounded to 2 s; `null` if it cannot be read in time |
| `uptime_s` | Seconds since the process started |

## Ports

| Service | Address |
| --- | --- |
| Dashboard (Phase 5) | 127.0.0.1:4400 |
| Gateway | 127.0.0.1:4401 |
| PostgreSQL | 127.0.0.1:4402 |
| MCP servers (Phase 2) | 127.0.0.1:4410–4412 (4412: ticketing) |

The test-only echo server has no published port.
