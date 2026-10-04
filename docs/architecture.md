# Architecture

AI Gateway sits between AI clients and the internal tools of a company, exposed over the
Model Context Protocol (MCP). The company in this repository, Harborline Supply Co., is
fictional, and all of its data is synthetic.

Every request is authenticated, then passes through one ordered chain of checks. Each
check can be set to enforce, monitor or off through configuration, with no code change.
That is what makes the project's headline result possible: a red-team scorecard showing
the attack success rate with each defense layer on and off.

This document describes Phase 1, the skeleton, Phase 2, the three MCP servers behind it
(ticketing, CRM and the handbook), Phase 5's telemetry (the dashboard that reads it comes
next), and the seams later phases build on.

## At a glance

| Topic | Decision |
| --- | --- |
| MCP revision | Targets **2025-11-25**, the newest revision with the initialize handshake and sessions. Older handshake revisions are negotiated by the SDK. Requests that declare the stateless 2026-07-28 revision get a clear 400; current clients then fall back to the handshake. |
| MCP SDK | The official Python SDK, pinned to `mcp==2.2.0` |
| Tool names | `<namespace>__<tool>`, e.g. `echo__say` |
| Authentication | Bearer tokens, stored as SHA-256 hashes and compared in constant time. It runs before the chain, and no configuration can turn it off. |
| Authorization | Tool-level scopes per client, enforced by the `scope` layer |
| Data store | PostgreSQL, accessed through psycopg 3 and plain SQL migrations |
| MCP servers | Low-level `mcp.server.Server` with strict tool schemas, one Postgres schema and role each, a service credential in front. See [MCP servers](#mcp-servers-phase-2). |
| Tool effects | `read` or `write`, from a reviewed policy table. A tool with no policy is a write. |

## Request flow

```
AI client ──HTTPS──► bearer auth ──► protocol version guard ──► MCP endpoint /mcp
                    (always on)                                      │
                                         resolve tool name in the cached catalog
                                         (unknown: same answer as out of scope)
                                                                     ▼
              ┌──────────────── pipeline, one run per request ────────────────┐
  tools/list  │ filter_tools:  scope → … → pinned descriptions                │
  tools/call  │ before_call:   scope → allowlist → rate limit → schema →      │
              │                egress → canary → classifier → approval        │
              │ ── forward exactly the checked arguments to the upstream ──   │
              │ after_call:    egress → canary → classifier (on results)      │
              └───────────────────────────────────────────────────────────────┘
                 every layer's verdict → one decision record → event sink + span
```

Phase 1 implements `scope`. Phase 3 adds `allowlist`, `rate_limit` and `approval` (3a, 3b and 3c:
see [Allowlist and rate limits](#allowlist-and-rate-limits-phase-3c) and
[Approvals](#approvals-phase-3b)); the login throttle in front of the pipeline is
[Failed logins](#failed-logins-phase-3c). Phase 4b adds `schema`, `pinned_descriptions`, `egress`
and `canary` (see [Injection layers](#injection-layers-phase-4b)); the classifier follows in 4c.

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
  configuration, and the approval's id when a person approved the call. It never holds arguments,
  results or credentials (the one place arguments are kept is the approver's copy, see
  [Approvals](#approvals-phase-3b)). The scorecard is
  computed from these records, not from the errors clients see.
- **Recording is best effort.** It never changes what the client gets. A record that cannot
  be built, a sink that raises and a sink that does not answer within 2 seconds are logged
  at ERROR on `ai_gateway.pipeline.runner`, with the request id, and the request goes on;
  cancellation is not swallowed. A request can therefore end with no record, and that log
  line is the only trace of it. The 2-second bound cannot interrupt a sink that blocks the
  event loop without awaiting, so a sink must not. The same holds for the
  `gateway.auth_failure` event: the 401 is sent whether or not the event was recorded, and
  neither a raising nor a stalled sink delays it.

### Configuration

The configuration lives in `config/pipeline.toml`, whose path is set by
`GATEWAY_PIPELINE_FILE`. It is read once at startup:

```toml
[layers]
scope = "enforce"        # enforce | monitor | off
allowlist = "enforce"    # value rules on a client's arguments (config/allowlist.toml)
rate_limit = "enforce"   # token buckets per client (config/rate_limits.toml)
approval = "enforce"     # last before forwarding; a write needs a person's approval

[safety]
allow_floor_override = false
```

- **Mistakes stop startup.** An unknown layer, mode or key stops the gateway from starting.
- **Fail safe.** A layer the file does not mention runs in `enforce`.
- **Floor layers are guarded.** `scope` and `approval` are floor layers: setting either to
  monitor or off needs `allow_floor_override = true` and logs a warning. In `monitor` the
  approval layer reports that a write would have needed approval, asks nobody, and lets it go on.

To produce the scorecard, the red-team harness writes one file per column and restarts the
gateway between runs. There is no runtime or per-request switch for an attacker to flip.

### Errors clients see

- **Unknown or out-of-scope tool:** JSON-RPC `-32602`, `Tool 'x' is not available to this
  client.` It is the same text in both cases, so it cannot be used to discover tools.
- **Blocked by any other layer** (the allowlist, an audit log that cannot take a write): `-32010`,
  `Request blocked by gateway policy.`, with a request id. The layer name appears only in the
  decision record.
- **Rate limited:** `-32010`, `Too many requests. Retry in N seconds.` The client may know how
  long to wait; it learns nothing else about the limit.
- **Too many failed logins:** HTTP 429 with `Retry-After`, before any secret is looked at.
- **A write waiting for approval:** a tool result with `isError: true` and structured content
  `{"status": "approval_pending", "approval_id": ...}`: nothing was forwarded, a person has been
  asked, and the client retries the same tool with the same arguments. It is recorded as a block
  (`blocked_by: approval`, `deny_code: approval_pending`).
- **A write an approver rejected or that expired:** `-32010` with `An approver rejected this
  call.` or `The approval for this call expired.`
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
registry holds the variable's *name*, never the value, and the value is never logged. The
name must end in `_SERVICE_TOKEN` (a database constraint, checked again when connecting), so
a registry writer cannot make the gateway send another secret, such as its database URL, to
a server of their choosing.

A server refuses to start with a credential shorter than 32 characters or containing
`change-me` (the placeholders of `.env.example`). The one extra quick-start step is
`python3 scripts/init_env.py`, which writes `.env` with a fresh `secrets.token_urlsafe(32)`
for every placeholder (database URLs get the new passwords too) and will not overwrite an
existing `.env` without `--force`. A `--force` run changes the database passwords in `.env`
but not those of roles that already exist in an existing Postgres volume; keep the old
database passwords in that case, or recreate the volume.

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

## MCP servers (Phase 2)

Harborline Supply Co. is fictional, and so is everything its servers hold. Three servers
run behind the gateway, from one image (`servers/Dockerfile`), each with its own Postgres
schema and role:

| Server | Port | Gateway namespace | Holds |
| --- | --- | --- | --- |
| Handbook | 4410 | `handbook` | 30 fictional handbook documents, searched by meaning and keywords |
| CRM | 4411 | `crm` | Accounts `ACC-00001` to `ACC-00040`, contacts, deals and activity notes |
| Ticketing | 4412 | `tickets` | Support tickets and comments for the same accounts |

They publish no port on the host. See [Networks](#networks) below.

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
| `list_tickets(status?, priority?, account_id?, limit, offset)` | read | Up to 20 ticket summaries, newest activity first; `offset` (0 to 10000) reaches older ones |
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

### The CRM server

Read-only, three tools, all with `read` policies:

| Tool | Does |
| --- | --- |
| `search_accounts(query, tier?, region?, limit, offset)` | Up to 20 account summaries whose name, industry or description contains the query; a name match ranks first |
| `get_account(account_id)` | The account's public fields, its contacts, its 10 newest activity notes and `notes_total` |
| `list_deals(account_id?, stage?, limit, offset)` | Up to 20 deals, newest close date first, **without the floor price**; money is integer cents |

- **Internal columns are walled off twice.** `credit_limit_internal`, `risk_rating_internal`,
  `internal_notes` and a deal's `floor_price_cents` are never selected and have no output
  field. And `crm_app` has column-level `SELECT` on the public columns only, so even
  `SELECT *` or a query bug is refused by the database. Grants are re-applied by
  `harborline-setup` on every run and start by revoking everything, so a widened grant is
  narrowed again.
- **Seed.** Deterministic (`random.Random(20261002)`, a fixed base time): 40 accounts, 120
  contacts, 60 deals, 200 notes. See `servers/crm/data/README.md`.

### The handbook server

Read-only, two tools, both with `read` policies:

| Tool | Does |
| --- | --- |
| `search(query, category?, limit)` | Up to 10 documents (default 5), each with its best-matching passage (at most 400 characters), best first |
| `get_document(document_id)` | A published document's title, category, date and full text; a superseded one also names the edition that replaced it |

- **Restricted documents are excluded in the database, before ranking.** The three
  restricted documents sit in base tables (`handbook.documents`, `handbook.chunks`) that
  `handbook_app` cannot `SELECT`. It can read only two views, `published_documents` and
  `searchable_chunks`, whose `WHERE classification <> 'restricted'` runs with the owner's
  rights (and which are security barriers). Restricted text is therefore unreachable by the
  server's role even with a bug in a query. The search SQL also repeats the condition inside
  each ranking step, so the exclusion happens before ranking and `LIMIT`, never afterwards.
- **Superseded editions are not searched.** A document's `superseded_by` front matter names
  the edition that replaces it (DOC-007, the 2025 returns policy, by DOC-008). The
  `searchable_chunks` view leaves such a document out, so no ranking step can return it, but
  `get_document` still serves it with `superseded_by` and a notice, because an old edition
  can still be the rule (DOC-008 keeps the earlier rules for orders delivered before 1
  February 2026). `harborline-setup` refuses a pointer to a missing, restricted or itself
  superseded document, `published_documents` shows a pointer only when its target is
  published, and the markers are synced from the files on every setup run, because the seed
  itself runs only when the schema is empty.
- **A restricted id looks like a missing one.** `get_document` reads the view, so a
  restricted id returns the same error, `Document 'DOC-xxx' was not found.`, as an id that
  does not exist.
- **Hybrid search.** The top 20 chunks by cosine distance to the query's embedding and the
  top 20 by `ts_rank` of `websearch_to_tsquery` are fused with reciprocal rank fusion
  (`k = 60`); each document is then represented by its best chunk. The vector column
  (`vector(256)`) has no index: a couple of hundred chunks are scanned exactly. Nor has the
  keyword column: the searchable view is a `security_barrier` and `@@` is not leakproof, so
  a GIN index could not be used through it, and a scan of about 150 chunks takes about a
  millisecond (revisit when the handbook reaches thousands of chunks, keeping the barrier).
  Documents are cut at `##` headings into chunks of about 800 characters, overlapping by
  about 120 within a long section, every chunk carrying its heading.
- **Embedding model: `minishlab/potion-base-8M`.** A static model2vec model: MIT licence,
  256 dimensions, about 30 MB, no GPU, no PyTorch, deterministic, and it embeds a query in
  well under a millisecond. It is pinned to revision `bf8b056651a2`, downloaded at image
  build time by `scripts/fetch_model.py` and verified against SHA-256 values committed in
  `scripts/potion-base-8M.sha256`, then loaded from `/opt/models/potion-base-8M`
  (`HANDBOOK_MODEL_PATH`) with the hub switched off: **no network at run time**. Setup
  embeds the chunks once; the server embeds only each query. The tests and CI fetch the same
  files into a cache directory with the same script. `servers/handbook/evals/retrieval.toml`
  holds 15 query and expected-document pairs; a test requires recall@3 of at least 0.8.
- **Documents.** Written for this repository, in the plain repository folder
  `servers/handbook/documents/`, which is in no Python package and in no image:
  `.dockerignore` keeps it out of the build context, so the restricted text is not on the
  server's filesystem to read, import or leak. Only the `servers-setup` one-shot has it,
  mounted read-only at `HANDBOOK_DOCUMENTS_PATH`; the loader, chunker and seed live in
  `harborline_setup`, which no server imports. CI greps the built image for the restricted
  documents' code phrases (`scripts/check_image_has_no_restricted_text.sh`) and fails if
  any is found. See `servers/handbook/data/README.md`.

### The fictional-data notice

Every CRM and handbook result carries a `notice` field (`mcp_common.notice`) stating the
data is fictional. The ticketing results, from Phase 2a, do not carry it yet; they say so in
the server's instructions instead.

### Schema and role isolation

Each server owns one Postgres schema and one role, and no role can see another's data:

| Role | Schema | Rights |
| --- | --- | --- |
| `gateway_app` | `public` (the registry) | read, and update `client_tokens.last_used_at` |
| `crm_app` | `crm` | `SELECT` on the public columns only (not credit limit, risk rating, internal notes or floor price), and on `schema_migrations`. No writes of any kind. |
| `handbook_app` | `handbook` | `SELECT` on two views and on `schema_migrations`; no base table. No writes of any kind. The pgvector extension lives in this schema. |
| `ticketing_app` | `ticketing` | read all tables; insert only the columns `create_ticket` and `add_comment` set (not `internal_notes`, not a comment's `visibility`, which defaults to `public`); update only `tickets.status`, `assignee` and `updated_at`; use the two sequences. No delete, truncate, create or temporary tables. |

`REVOKE ALL ON SCHEMA <schema> FROM PUBLIC` keeps every other role out, and the three server roles cannot read each other's schemas, the registry or `public`. `harborline-setup`
also revokes `CONNECT` and `TEMPORARY` on the database and `USAGE` on `public` from `PUBLIC`,
then grants `CONNECT` to `gateway_app` and each server role and `USAGE` on `public` to
`gateway_app` by name, so no server role has anything in `public`. Because of that,
setup creates the pgvector extension **inside the `handbook` schema** (`CREATE EXTENSION IF
NOT EXISTS vector WITH SCHEMA handbook`, as the owner) rather than in `public`. (Other databases of the
cluster keep their own defaults; this stack uses one.) Tests assert each of these, including
the column-level grants.

The `harborline-setup` one-shot (the `servers-setup` Compose service) runs as the database
owner and is safe to repeat. For each of the three servers it creates the role if missing, then `ALTER ROLE ... PASSWORD`
with a quoted literal (so it also rotates the password), creates the schema, runs the
schema's migrations, **applies the role's grants on every run** (not in a migration, so they
never depend on when the role was created) and seeds the schema when empty. A role cannot only
live in `db/init`, which runs once on a fresh volume, so existing volumes get it from this
step. Each server process connects only as its own role.

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
  the gateway's own verdict, and it is the only annotation clients see: the upstream's
  `destructiveHint`, `idempotentHint`, `openWorldHint` and titles are dropped.
- Policies are re-read every registry poll (5 s), so a change applies within seconds.
- Every decision record carries `effect` and `effect_source`. Phase 3's approval layer keys
  on the effect.

### Client identity in `_meta`: attribution, not authorization

On every forwarded `tools/call` the gateway sends only its own `_meta`:
`{"io.aox.ai-gateway/client": "<authenticated client name>"}`. Every key the client sent in
`_meta`, including that same key, is dropped and never forwarded, so a client cannot claim
to be another one.

One exception is not a key the gateway forwards but trace context. The gateway's own trace
context rides along on the upstream call, and it is always a trace the gateway started: a
client's `traceparent`, `tracestate` and `baggage` are not forwarded (see
[Fresh trace at the gateway](#fresh-trace-at-the-gateway)).

A server may record the client name from `_meta`, for example as a ticket's `requested_by`, and
uses `direct` when the key is absent or not a valid client name. **It is attribution only.**
Anything that can reach a server directly can write any value there, so a server must never
use it to decide what a caller may do. Authorization is the gateway's scope check.

## Seams for later phases

The seams that the shared `agent-core` library fills (approval queue, audit log, tracing) are
filled: the gateway pins agent-core's first release, `v0.1.0`, by tag, and imports it. This
repository copies none of its code.

| Seam | Phase 1 | Later |
| --- | --- | --- |
| `EventSink` (`seams/events.py`) | Writes JSON lines to the log and, with a telemetry database, queues rows for it. Events already follow the audit log's record shape: dotted action, actor id `client:<uuid>`, subject, a small payload with no secret-named keys. | The audit log is not an `EventSink`: it is a seam of its own on the pipeline, because it can refuse a write ([Audit](#audit-phase-3)). |
| `ApprovalGate` (`seams/approvals.py`) | Protocol only | Filled in Phase 3b by `policy/approvals.py`: see [Approvals](#approvals-phase-3b). |
| Tracing | The OpenTelemetry SDK, configured when a telemetry database is set: the gateway's spans are queued and stored in Postgres (see [Telemetry](#telemetry-phase-5)). Span names are in `telemetry/attributes.py`. | Another processor, such as an OTLP exporter, can be added without changing the gateway. |

## Telemetry (Phase 5)

Every request leaves a decision record and spans. They are stored in Postgres, in a `telemetry`
schema of their own, and a dashboard (Phase 5c) reads four views of it. **Nothing here ever
holds a tool argument, a tool result, a credential or a client address**: the tables have no
column for them, the rows are built from named fields of a record and never from the record
as a whole, only the gateway's own spans are kept with an allowlist of attributes, and every
free-text column is bounded by a CHECK. An end-to-end test sends a marker argument through the
gateway and finds it in no column of any table.

### Audit (Phase 3)

Every tool call is recorded in agent-core's append-only, hash-chained audit log, in the `policy`
schema. agent-core is pinned by tag (`v0.1.0`, in `gateway/pyproject.toml`); the log and, from
Phase 3b, the approval queue are its. This is not telemetry. Telemetry is best effort and tells the
dashboard what happened; the audit log is the record of what the gateway did, and a write is not
made without it.

### What is recorded

- **Every `tools/call`**, whatever its outcome (forwarded, blocked by a layer, blocked because the
  tool does not exist), as `gateway.tool_call`: the client, tool, namespace, effect, outcome, the
  layer and code that blocked it, each layer's verdict, the argument **hash**, the pipeline's
  fingerprint and integer microseconds. No argument, no result.
- **Before a write is forwarded**, `gateway.call_started`: the client, tool and argument hash. It is
  appended after every layer has passed the call and before the upstream is called.
- **Gaps in the log itself**, as `audit.gap` (see below). Approvals (Phase 3b) write their own events
  in the same transaction as the change they describe.
- A call to a tool that does not exist is recorded too; if the name the client chose cannot be an
  audit subject, the record has no subject and holds the name's hash. A call the client cancels while
  it is being forwarded is not recorded as a `gateway.tool_call`, so a write cancelled then leaves
  only its `call_started`.
- Not `tools/list`, and not each failed login: an attacker would choose how fast the log grows.
  They stay in telemetry; a rate-limit trip (Phase 3c) is audited once.

agent-core's payloads take no floats and refuse secret-shaped keys, and the log scans strings for
secrets; durations are integer microseconds, and the records carry hashes and codes only. A test
sends a marker argument that the echo tool also returns, and finds it in no record.

### When the audit log cannot be written

| Call | Policy | Why |
| --- | --- | --- |
| **A write** | **Refused.** The write-ahead record is appended and waited for (the request stops waiting after 2 s). If that fails the call is blocked as `blocked_by: audit`, code `audit_unavailable`, with the generic policy message, and the upstream is never called. | A write with no record of its attempt is worse than a refused write. |
| **A read** (and every call's decision record) | **Proceeds.** The record is queued and never waited for. | A read is never refused because the log is down. |

The queue holds 5 000 records and drops the oldest when full, counting what it dropped. When the log
is back, the first record written is an `audit.gap` stating how many records were lost, so the log
itself says it is incomplete. `[safety] allow_unaudited_writes = true` in `config/pipeline.toml`
lets writes through without a record (it logs a warning, and is off by default); the same
applies when no audit database is configured at all. `/healthz` reports `audit: {status,
queue_depth, dropped_total, rejected_total, written_total, write_ahead_failed_total}` and never
becomes unhealthy over it: a failing log already refuses writes, and a restart would not help. The
status is `degraded` while queued records cannot be written and until a failed write-ahead record
is followed by one that succeeds.

**How this coexists with telemetry.** They are two seams fed from the same decision record, and
neither can block the other. Telemetry goes through the `EventSink`, awaited with a 2 s bound,
always best effort. Audit is owned by the pipeline: the write-ahead step can refuse a write, and
the rest is a queue. Telemetry down with audit up, audit down with telemetry up, and both down are
all tested.

**Cost.** agent-core appends one event per transaction and serialises appends. Measured on this
node on 2026-10-03 under v0.1.0 (`scripts/measure_audit_throughput.py`, 50 appends and 5 runs, the
stack's own Postgres on a disk volume, a copy of the real one): one append at a time, median 16.8 ms
(p95 55.6 ms); 50 appends at once took 0.78 s (median of 5; 3.2 s under a3, with a median of 22 ms and a
p95 of about 100 ms for one); one `append_many` of 100 took 75 ms (about 150 ms under a3, with
`append_in`). An approval's submit takes 20.9 ms (p95 72.7 ms), a repeat of an open one 5.4 ms (p95
9.6 ms: the idempotent path a retry takes), and a cancel 19.4 ms. The recorder therefore still writes
the queue in batches of up to 100 in one transaction, using `SQLAuditLog.append_many`. Only the
write-ahead record waits for its own transaction. agent-core does not coalesce independent appends
(it lists that as planned work), so the batching stays. The test Postgres in agent-core's own
benchmark runs on tmpfs, so its numbers are lower than these.

**Bounded waits.** agent-core's storage is async, on a psycopg connection pool. The gateway gives
the audit log pools of its own (four connections for the write-ahead record, one for the batches),
so a stalled audit log fills only them, and a write is refused at once when all four write-ahead
connections are taken (`busy`). A caller stops waiting at its limit (2 s for the write-ahead record,
15 s for a batch) and its connection is closed; what ends a statement the server is still running
(a wait for the audit append lock, say) is the database's own limits: a 2 s connect timeout, then
lock, statement and transaction timeouts (1.5, 1.8 and 1.9 s for the write-ahead record; 5, 8 and 12 s
for the batches; the transaction timeout needs PostgreSQL 17). This narrows the chance that a write
refused for time is committed late; it does not remove it, since a statement that starts just before
the limit can still commit after the request has moved on. A batch whose outcome is unknown (a
timeout, or a connection lost after COMMIT) is checked against the records written since the last
one known, by `record_id`, before it is retried, and its gap record is built once, so a retry does
not store either twice.

### Roles, and the approval guard

| Role | Can |
| --- | --- |
| `policy_gateway` (the gateway) | read and append the audit log; create approval requests (pending only) and consume an approved one (not one whose approver has been removed: it may ask whether an approver is still active, and nothing else about them) |
| `policy_approver` (a group: nobody logs in as it; each approver's own login is a member, see [Approver identity](#approver-identity-phase-5c-1)) | read and append the audit log (a login writes the audit event of its decision); decide a pending request (only as the principal the owner mapped to its login, see below); read the approvers and the stored arguments |
| `policy_auditor` | read the audit log and which login is which approver, and nothing else (never the arguments or the approvers' names) |
| `policy_payload_purger` (one login, `approvals-purge`) | a member of the approver role with no `SET`, one connection; it purges stored arguments after 7 days and is mapped to a principal that is not an approver, so the gate uses no decision it writes (an approval or a rejection) and `audit-verify` refuses its decisions |

A write's arguments are stored with its request (`include_payload`), where the approver reads them;
the auditor and the dashboard's reader cannot. The gateway role can read the stored arguments of every request (agent-core's requester
layout; it must, to return an open one, and it had them to begin with), so a stolen gateway
credential yields the last 7 days of them, not only live traffic.

agent-core v0.1.0 ships the two roles and the guard itself. Its installer (`gateway-admin
policy-setup` runs it every time: it is idempotent and upgrades an older schema in place, keeping
every row and every audit record, and no anchor stops verifying) creates the tables in the `policy` schema for a *requester* role (`policy_gateway`) and an
*approver* role (`policy_approver`), grants each only its layout (the requester may update `status`,
`consumed_at` and `closed_at`; the approver the decision columns), and installs a guard trigger that
allows only these changes, using the database's own clock and role membership:

| From | To | Role |
| --- | --- | --- |
| (insert) | pending | requester |
| pending | approved or rejected | approver, not the requester |
| pending | cancelled | requester |
| pending | expired | requester or approver, once past its lifetime |
| approved | consumed | requester |

Everything else is refused, including any change to what a request authorises, DELETE, TRUNCATE and
a superuser or the owner (they are members of every role, so they are neither side). The gateway
cannot decide an approval by any route, whatever its code does. This replaces the guard 3a wrote.
3a's guard tests were run against a3's guard (after the tests' own raw SQL was made valid for a3,
with a positive control, so a refusal comes from the clause under test), and every attack was
refused. The differences, as gaps in what 3a enforced, are these, and none lets the gateway approve:
- a role that is a *member* of `policy_gateway` counts as the requester side, and a member of
  `policy_approver` as the approver side (3a refused both by name). A member has exactly that
  role's powers, and `policy-setup` revokes every membership in the policy roles on each run, except
  an active approver's login in the approver role: that is a point-in-time control, not a
  continuous one;
- 3a refused the owner and a superuser outside the two roles by name; a3 refuses them as neither
  side, which is stricter (the tests no longer rewrite a request's lifetime as the owner).

`policy_auditor`, the arguments table, the approvers, the purge function and the dashboard's view
are this gateway's own, and `policy-setup` takes back anything the gateway or approver role holds
beyond a3's layout. An approved request that no approver's decision approves (plain SQL could make
one under a2) is cancelled by the upgrade and logged. The a3 database layer ignores `search_path`,
so every query here is schema-qualified. The audit table's own protections are agent-core's:
triggers that refuse `UPDATE`, `DELETE` and `TRUNCATE`, and a check that the connecting role cannot
do any of them. Audit rows carry the database role that wrote them (`db_role`, audit schema 3). The
database owner can disable the triggers, which is what the anchor test does; the chain and an
anchor are what show it.

### Approvals (Phase 3b)

A write waits for a person. The `approval` layer runs last, so a person approves exactly the call
that is forwarded; a read never asks.

1. The gateway looks for this client's newest unexpired request for this tool and these arguments
   *and this upstream* (the hash of `{tool, {arguments, upstream}}`, where the upstream is
   `UpstreamServer.identity`: its id, namespace, address and credential name). Repoint the
   namespace at another address or credential and an approval given before it no longer applies. If there is none it submits one (30 minutes to live,
   `settings.approval_ttl_s`), asking for the role that `config/approval_roles.toml` lists for that
   tool, with no delegates, and stores the arguments for the approver. A write with no listed role
   is refused: nobody may approve it.
2. It **holds** the call for up to 45 s (`approval_hold_s`, polling once a second: agent-core has
   no wait/notify) for a decision. At most 16 calls are held at once and at most 4 by one client
   (`approval_max_holds`, `approval_max_holds_per_client`), so one client cannot fill every wait
   slot; past either limit a call is answered "pending" at once and the client retries.
3. **Approved:** the approval is consumed, once, and the call goes on to the audit write-ahead and
   the upstream. The gateway first checks that the request was made by *this* client, and agent-core
   agent-core's consume checks the tool, the arguments and the requester again. One approval authorises one run.
4. **No decision yet:** the client gets the pending result and retries the same call; the retry
   finds the same request and holds again. **Rejected:** the rejection stands until the request
   expires, so a retry does not ask again. **Expired:** the next call asks afresh.
5. **The queue cannot be used** (no policy database, a dead database, arguments over 64 KiB):
   the write is refused. The layer fails closed.

**One open request per intent.** Identical calls from a client share one request. That is agent-core's
`submit`, which is idempotent: while a request for the same client, tool and arguments is open
(pending, or approved and not yet used) it returns that one and asks nobody again, and a unique
index over open requests makes it hold when calls race. An approved request that runs out unused is
closed as expired by the next submit, so it never blocks the same write for good, and a used one is
closed, so the next identical call asks again. A repeat that differs in role, lifetime, delegates or
summary is `ApprovalConflictError`, never a reuse: the gate refuses it (`unavailable`, logged), so
changing an approval setting takes effect once the old requests expire.

Three things stay the gateway's. **A rejection stands until it expires**: agent-core counts a rejected
request as finished, so the gate looks for one first and a retry does not ask the approvers again.
**The summary is a pure function of the tool and its arguments** (`approval_summary`: the tool and a
short digest, no client name, no time, no argument text), because a different summary on a repeat is
a conflict; a test sends two retries from differently named clients and requires the identical
text. And **a request the previous release left open** differs from this one in its summary and in
having no stored payload, the only conflict the gate repairs: if nobody has decided it, it is
withdrawn and asked afresh (logged); if a person approved it, it is used as it is, since consuming
checks the tool, the arguments and the requester. An upgrade is therefore best done with no write
pending; the open requests of the old release cannot be shown to an approver, who can only reject
them.

`expire_due()` runs once a minute and stores `expired` on requests past their lifetime; reads treat
such a request as expired whether or not it has run.

Who may decide is not the requester's choice: the approver's tool builds agent-core's
`RoleApproverPolicy` from the same `roles_by_action` file, so a request for an unlisted action, or
with another role than the file names, is refused. `trust_requester_role` is never used, and a test
fails if it appears outside the tests.

An approval is for exactly one tool with exactly those arguments for the client that asked: other
arguments are a new request; another client's request is never found, and a direct attempt to use
it is refused.

**The arguments are kept, once.** So that a person approves what the call really says, the full
arguments of a write awaiting approval are stored with the request, in its payload, and its
`payload_sha256` binds them (agent-core refuses to read a request whose stored payload does not match
its hash). It is the one exception to "never store arguments". They are never in an audit record or in
telemetry. `approvals-purge` (its own Compose service and login, hourly) calls agent-core's
`purge_payloads` for requests that finished more than 7 days ago (consumed, rejected, cancelled or
expired; never one still open): the payload goes, the hash stays, and one `approval.payload_purged`
audit record names each request, written by the purge's own login, which `audit-verify` accepts from
that login alone. A purged request reads as purged, not as never stored, and can only be rejected.
The purge must run as the approver side (agent-core's rule), so its login is a member of the approver
role: it is mapped to a principal that `policy.approvers` does not list, which is why no decision it
writes is used by the gate or accepted by verification.

**`gateway-approver`** (`docker compose run --rm approver list | show | approve | reject | whoami`)
is a person's tool, and it takes no name: who is deciding is read from the database session (the
approver whose login it is, [below](#approver-identity-phase-5c-1)), who must be active and hold the
request's role (`approver`). The tool checks the role; so does the gate, from the approver's record,
so a login that approves in plain SQL without the role does not get its approval used. Before it shows a request as approvable, the tool
parses the arguments back from the text it is about to display and hashes them with the tool name:
the hash must be the one stored when the gateway asked, or it refuses (a request whose arguments
were changed, or purged, can only be rejected). The arguments are shown as JSON with every
non-ASCII and control character escaped, and anything from outside (names, summaries) has control
characters and ANSI sequences removed, so nothing in a call can redraw the approver's screen. The
upstream is shown by name (the namespace in the tool's name, `tickets` for `tickets__assign`) beside
its identity digest: the digest is what was hashed into the request and cannot be read back, so the
name comes from the tool.

`policy.dash_approvals` is the dashboard's view of the requests (tool, the upstream's namespace,
client, status, who decided and their display name, times) with no arguments and no free text; the
dashboard's reader role is granted that view and nothing else in the schema.

Audit records carry the database role that wrote them (set by a trigger), and `audit-verify`
fails a `gateway.*` record, an `audit.gap`, an `approval.requested`, an `approval.consumed` or an
`approver.*` record that the gateway role did not write, and an `approval.resolved` that no approver's
login (or, in a log from before the logins, the approver role) wrote: the approver role may append to
the audit log, and without this a holder of its credential could add records the gateway never wrote
under a chain that still verifies. It also fails a second decision on one request, and an
`approval.payload_purged` that the purge's login did not write. The shared approver role's decisions
are accepted only before the first `approver.*` record (they are from before the logins).

Whose decision it says it is, is no longer read back from the log: with login binding on (below) the
database refuses a decision whose `resolved_by` is not the principal mapped to the login that made
it, so `audit-verify` no longer compares the two. The gate still refuses an approval whose audit
record was not written by an active approver's login holding the role the request needed.

What is *not* here: the approval is consumed before the audit write-ahead, so a write refused because
the audit log is down has used up its approval (the client asks again). `scripts/auto_approver.py`
approves for the scenario and the simulator; it is test tooling, outside the gateway, and needs
`--approve-as` (which must be the login's own approver: it checks) and `LAB_AUTO_APPROVE=yes`.

### Approver identity (Phase 5c-1)

Each approver has a database login of their own, and the tool reads who they are from it. The
audit trigger sets `db_role := current_user` (and, from agent-core's audit schema 4, `db_login :=
session_user`) on every record, so a decision is recorded with the login that wrote it, whatever the
tool claims; `approvers.db_role` maps the login to the person.

**Ids are opaque.** An approver id is `appr_` and ten random characters, made by `approver-add`; the
login is `policy_approver_<id>` and the principal `human:<id>`. The audit log and agent-core's login
mapping are append-only and cannot be erased, so nothing written to them may be a person's name: the
name goes in `--name` and lives in `policy.approvers.display_name` only. `approver-add` refuses an id
that is not of that shape (the one exception is `lab-approver`, which is not a person and is made by
`policy-setup`).

**Login binding.** `policy-setup` installs agent-core's guard with `bind_resolved_by=True` and, as
the owner, maps each active approver's login to its principal (`agent_core_approver_logins`, which
only the owner, connected as itself, can write; `approver-add` maps a new login and `approver-remove`
ends its mapping). The guard then judges `session_user` and refuses a decision whose `resolved_by`
is not the principal mapped to it: by the library, by plain SQL and after a `SET ROLE` (a login has
no `SET` on the group or on another login). A login with no mapping can read the queue and never
decide. A mapping is for good: the login and the principal are never mapped again, even after the
mapping ends, and it is by the role's OID, so a login made again could not decide. Rotating a login
whose role is gone is therefore refused (remove the approver and add them again), and the lab
approver's role is kept (shut, with no membership, when its password is unset) rather than dropped
and made again. A setup that cannot map an active approver says so in the log and goes on.

| Command (`gateway-admin`, as the owner) | Does |
| --- | --- |
| `approver-add [<id>] --name N [--role R]` | Records the approver (a new, opaque id unless the id of an existing one is given) and makes `policy_approver_<id>`, and maps it. Prints the id, the login and a generated password **once**; nothing the gateway runs keeps it (but it is on the
admin container's stdout, which a non-default Docker log driver may keep, and `-e APPROVER_PASSWORD`
is visible through `docker inspect`: prefer the prompt). Adding an active approver again only updates the name and roles. |
| `approver-rotate <id>` | A new password, shown once; the old one stops working and the open sessions end. Refused if the login's role is gone (its mapping cannot be made again). |
| `approver-remove <id>` | Ends the sessions, ends the mapping and drops the login. The row stays, inactive and marked removed. |
| `approver-list` | Each approver, their roles, state and login. |

A login is `LOGIN` with no other attribute, `CONNECTION LIMIT 2`, a password valid for 90 days
(`VALID UNTIL`; rotate before then), the 5 s idle-in-transaction limit, and exactly one membership:
the approver role, `WITH INHERIT TRUE, SET FALSE, ADMIN FALSE`. Inheritance is how it connects and
reads the queue; without `SET` it cannot `SET ROLE` to the group, and without `ADMIN` it cannot
grant the group on. Setup takes back any other role, any role that is a member of the login (it
could `SET ROLE` to it and write as it) and any table privilege granted to it directly; the
`CONNECT` that `ensure_role` gave it stays. The lab login is made the same way. The shared approver role itself is `NOLOGIN`
and there is no shared approver password: `POLICY_APPROVER_DB_PASSWORD` is gone. The tool signs in
with `APPROVER_LOGIN` and `APPROVER_PASSWORD` (or asks), never from `.env`.

`policy.approvers` is the map of who is who, which feeds agent-core's login mapping: one row per approver principal (`human:<id>`), one login per row, unique both
ways; only the owner writes it (no other role can insert, update or delete); a row is never deleted,
its login never changes once set, and a removed approver stays removed, so an id and a login are
never given to anyone else. A trigger enforces the last three (delete, truncate, change of id or
login, un-removing); the owner can disable triggers, as it can for the audit log's. `audit-verify`
reads the login map through a view of this table, so the map is as trustworthy as the owner: the
owner can rewrite the table and the map with it (agent-core's own mapping is the one the guard uses). What the hash chain does hold is the
`approver.added` records, which name each login.

**Audited.** `approver.added`, `.updated`, `.rotated` and `.removed` are appended to the audit log
with the approver's id, their login and roles: never the password, never their name. They are
appended as the gateway role, because agent-core refuses an append from the role that owns the
table; the actor is `admin`, which names the tool, since the admin CLI has no identity beyond the
owner's credential. An add whose record cannot be written is undone (the login is dropped).
`audit-verify` accepts these records from the gateway role only.

**Setup and removal.** `policy-setup` keeps each active approver's login as recorded (its
attributes, its one membership, no direct grants) and revokes every other membership of the policy
roles, as before. agent-core's installer counts a decision only while its writer is a member of the
approver role, so a removed approver's approved-but-unused requests are cancelled by the next setup
(when someone else's decision is on record). When the only decisions are by removed approvers the
installer would refuse; setup then goes on without cancelling, and still stops for an approval no
approver's login made (plain SQL). The gateway also refuses to use an approval whose approver is no
longer active, at once, so a removal does not wait for a setup: such a request answers
"unavailable" until it expires (30 minutes at most). The lab approver is the one login setup makes
itself, recorded as approver `lab-approver`, which `approver-add` refuses.

**Limits.** A login shows which credential decided, not which human typed it: share one and the log
cannot tell. The owner's credential makes and removes approvers, so it stays off approvers'
machines. Postgres does not throttle password guesses; the database is published on `127.0.0.1`
only, and passwords are 256 random bits.

### Allowlist and rate limits (Phase 3c)

Two layers sit between scope and approval, so a call that scope refuses costs nothing, and a call
the allowlist refuses takes no rate-limit token.

**`allowlist`** constrains the *values* of a client's arguments, for what a person's yes should not
be needed or enough for (`config/allowlist.toml`; the format is in
`pipeline/layers/allowlist.py`). A rule names a client (or `*`), an exposed tool and a top-level
argument, and gives `one_of`, `pattern` (the whole value must match; values over 4096 characters
never do), `max_length`, `minimum`/`maximum` or `required`. It never rewrites an argument. The
shipped rule: the support bot may open a ticket at any priority but `urgent`. A refusal is the
generic policy message, so a client cannot read the rules out of the errors; the decision record
holds `blocked_by: allowlist`, `deny_code: allowlist_violation`. A mistake in the file stops startup.

**`rate_limit`** keeps a token bucket in memory per client for reads, per client for writes (by the
tool's reviewed effect), and per client and tool for tools with a limit of their own
(`config/rate_limits.toml`). A bucket holds `burst` tokens and refills `burst` tokens every `per`
seconds. A call must find a token in every bucket that applies; a call this layer refuses takes none
(layers after it, such as approval, can still refuse a call that has taken one, so a client that
retries a pending write quickly spends write tokens on the retries). The state
is per gateway process and starts full; it is bounded (the least recently used bucket goes first).
The shipped limits are generous (600 reads and 60 writes a minute) except one deliberately tight
tool limit that the traffic simulator uses to show the layer working.

Both layers switch `enforce`, `monitor` and `off` like any other (they are not floor layers).

### Injection layers (Phase 4b)

Four layers sit between the policy layers and approval, in this order: **schema → pinned_descriptions
→ egress → canary**. Each switches `enforce`, `monitor` and `off`, none is a floor layer, and each
records a verdict with a code and, in a column of its own (`layer_verdicts.score`), an integer count
(violations found, values matched, canaries seen): never a name or a value. A refusal is the generic
policy message to the client; the layer is named in the decision record only. They run before
approval, so a call they refuse never asks a person.

| Layer | Detection | False-positive risk | What monitor mode records |
| --- | --- | --- | --- |
| `schema` | Arguments against the *pinned* input schema (the catalog's own for a tool with no pin), with a top-level `additionalProperties: false` forced; also NUL in any key or string, a non-finite number, nesting over 6, arguments over 64 KiB | A client that sends `"5"` for an integer, or an argument the server used to drop | `would_block`, `schema_violation`, the number of violations (at most 50) |
| `pinned_descriptions` | SHA-256 of name, description and input schema against `config/tool_pins.toml`. A drifted or unpinned tool is hidden from tools/list and refused on a call, and an alert is raised once per tool and definition | A legitimate deployment that changes a description hides the tool until it is pinned again | `would_block`, `pin_drift` or `pin_unpinned`; the tool stays visible |
| `egress` | Per MCP session, the record ids, emails and phone numbers that reads returned; a write is refused when it carries 5 or more of them, or 10 across the session's writes, or any internal-only marker. The record a call is about is exempt per tool | A legitimate write that cites more than a few records | `would_block`, `egress_bulk` or `egress_marker`, and the count of matching values, kept for allowed calls too |
| `canary` | A seeded decoy value (squeezed of case, separators and zero-width characters, base64 and hex runs decoded) in any call's arguments, found by hash | Near zero: a model quoting a decoy verbatim into a comment | `would_block`, `canary_hit`, the count of canaries |

**Pins.** `config/tool_pins.toml` holds each tool's reviewed description and input schema as text
with their hash, so a change to a description is a diff someone reads; the gateway refuses to start
when an entry's hash is not the hash of its own text, and a test fails when the file is not what the
servers define (`scripts/generate_tool_pins.py` rewrites it). It is read once at startup, so
re-pinning is a restart; a call carries the definition the catalog holds *now*, which is what is
compared. An alert is an ERROR in the log and a `gateway.alert` audit record (kind, tool, client,
hash or canary name), once per condition per ten minutes.

**What egress keeps.** A set of 64-bit keyed hashes per session, in memory, under a key that is random
per process: 2 000 values a session, 200 000 in all, 1 000 sessions, least recently used first. Never
an argument, a result or a value; nothing on disk, in the database or in telemetry; a restart forgets
it. A session past its cap stops noting values, which makes the score read low. It does not catch a
transformation of the data (a summary, an encoding) or a value the session never read.

**Canaries.** Two, in the fictional data: a code in the `about` of account `ACC-00001` and in the
description of ticket `TKT-000001` (on a fresh volume; an existing volume's data is not reseeded).
`config/canaries.toml` holds their names and hashes, never the values. They are public, since they
are in this repository: a real deployment makes its own and keeps the list secret.

**Known limits of these layers** (gatekeeper pass 1): the guard lets any login in the approver role
write a decision, so the payload-purge credential can reject or approve a pending request; the gate
counts only an active approver's decision (a rejection included), so the effect is that a client's
request is asked afresh, which a holder of the credential can repeat. A purge-only role in agent-core
would end it. The pins cover the description and the input schema, not the output schema or titles.
Egress and canary scan at most 256 KiB of a call's arguments. A session that read more values than it
is tracked for, or whose ledger was dropped for room, cannot write a value (`egress_state_lost`).
The service logins' 90-day expiry is renewed by every `policy-setup`, so it does not force a
rotation. The dashboard's sign-in redirect takes its scheme from the request URL: behind a TLS-
terminating proxy that does not forward the scheme it would point at `http://`.

**Startup cross-check.** The allowlist, the rate limits and the approval roles are checked against the
pins before the gateway accepts a request: a tool no pin names, an argument the pinned schema does not
have, a range on a string, a value outside an enum, each stops startup, naming every mistake.

### Idle transactions (Phase 3c)

Any role that can connect can take agent-core's one audit append lock, and a write that cannot be
audited is refused, so one session sitting idle inside a transaction while holding that lock could
stop every write. The limit protects against stalls and mistakes, not against a hostile role: a role
can `SET` the limit to 0 for its own session, and a session that holds the lock while *running* a
statement is not idle. (The lock is a public advisory lock that the server roles can also reach;
closing that, or moving agent-core to a row lock only writers can touch, is left.) Every role the setups create has `idle_in_transaction_session_timeout` set on it:
5 s for the policy roles (and the lab approver), 30 s for the rest (the gateway's, the telemetry
roles and each server's: `ensure_role` carries the default, a migration and `db/init` cover
`gateway_app`). The server ends such a session; a test takes the lock from each policy role, shows a
write blocked, the session ended within the limit, and a write then succeeding. The database
owner, used only by setup and administration, has no such limit.

### Failed logins (Phase 3c)

`auth/throttle.py` sits in the bearer-auth middleware, before the token is looked at:

- after 5 failures for one token lookup id within 60 s, that id is refused with a 429 for 60 s,
  without its secret being examined (and not counted again: refusals do not extend the lockout);
- when 200 failures have happened within 60 s, only a lookup id that authenticated in the last 15
  minutes (`login_known_good_ttl_s`) is served, until the failures age out: a spray of invented
  ids, which never trips the per-id limit, cannot lock out clients that were working. A request
  with no token at all does not count (MCP clients probe without one, and refusing it costs
  nothing). The memory of who logged in is lost when the gateway restarts, so during an attack a
  restart closes the gateway to clients until they have logged in once more.

The price of not answering a guess is that someone who knows a client's lookup id can lock that
client out for a minute at a time. The lookup id is printed once, when a token is issued, and is not
in any record a client can read. State is in memory, per process and bounded. Throttled attempts are
recorded as auth failures with reason `throttled`.

### The lab profile (Phase 3c)

Phase 6's red-team runs need a scripted attacker's writes to get through the approval layer so that
what is measured is the other layers. `docker compose --profile lab up` starts `lab-approver`,
which approves every pending write (`scripts/lab_approver.py`, mounted into that one service and in
no image). It is off unless asked for three times, and each is enforced:

1. the `lab` profile: the default stack does not start it (naming the service on the command line,
   `docker compose up lab-approver`, starts a profiled service without the profile, so this is a
   convention and not a fence);
2. `LAB_AUTO_APPROVE=yes` in the environment: the service exits at once without it (Compose
   resolves every service's variables whatever the profile, so it cannot be a required variable);
3. a database role that exists only when `policy-setup` is given its password
   (`POLICY_LAB_APPROVER_DB_PASSWORD`): `policy_lab_approver`, a member of the approver role, so it
   has the approver's powers and nothing else; the next setup without the password shuts it (no
   login, no membership, no grant) and keeps the role.

The role is reset on every setup (a grant or membership made by hand does not survive it), not
dropped and made again: agent-core's login mapping is by the role's OID and is never made twice. Its decisions are in the audit log under its own role name, which
`audit-verify` accepts for `approval.resolved` and for nothing else, and says so when it finds any:
a stack that was used as a lab is not quietly mistaken for one that was not. Setup records it as
approver `lab-approver` (inactive when the role is gone). Never use it
against anything real: it defeats approval. CI sets the switch on the Harborline step only, for the
test approver (`scripts/auto_approver.py`), and a test pins that.

### Tamper evidence

The hash chain detects an edit, but whoever can rewrite rows can rebuild every hash after it, and a
test does exactly that as the database owner: the chain still verifies. Only a head kept somewhere
the log's writers cannot reach shows the rewrite.

- `gateway-admin audit-anchor --file FILE` appends the chain's head (its sequence number and hash) to
  a file outside the database. It first verifies the chain and every earlier anchor, and refuses to
  anchor a log that fails them, so an anchor can never make a rewrite look like the truth.
- `gateway-admin audit-verify --anchors FILE` walks the chain, then checks every anchor against it:
  a rewritten record, a log cut short, and two anchors that disagree about one record all fail.

Both run as `policy_auditor` (`POLICY_AUDITOR_DATABASE_URL`). Keep the anchor file on a different
host, or at least a different account, from the database, and take an anchor on a schedule (cron is
enough): an anchor protects what came before it. Verifying 381 records took about 30 ms and the
command walks the log twice, so a million would take about three minutes. The anchor file is read
without following symlinks and refused if others can write to it. The Compose `admin` service has
a read-only root, so give `docker compose run admin audit-anchor` a mounted file
(`-v "$PWD/anchors:/anchors"`), or run the command from the host. Signed anchors or a timestamping service are not worth
it at this size.

## Data model

| Table | One row per | Holds |
| --- | --- | --- |
| `requests` | decision record (a `tools/call` or a `tools/list`) | time, kind, client id and name, tool and namespace, effect, `outcome` (`forwarded`, `blocked`, `listed`), `blocked_by` and `deny_code`, upstream status, total and upstream duration, the argument **hash**, protocol version, pipeline fingerprint, trace id |
| `layer_verdicts` | request × layer × hook | the layer's name, the hook (`filter`, `before_call`, `after_call`), its mode, the verdict (`allow`, `deny`, `would_block`, `off`, `error`), code, time, and an integer `score` some layers set (a count, never content) |
| `auth_failures` | failed authentication | time, reason, the token's lookup id (the non-secret part); not the address |
| `spans` | span of the gateway's own instrumentation | trace and span ids, parent, name, start, duration, status, request id, allowlisted attributes |
| `pipeline_configs` | configuration fingerprint | the layers and their modes, in order |

Indexes serve the dashboard's queries: `requests` by time (newest first, for keyset paging), by
client, by tool, and a partial index on blocked requests; `layer_verdicts` by layer and verdict;
a B-tree on the time of each table, which the purge's "oldest row" question and the time
ranges use. A tool no upstream offers is stored under its name (bounded, and a valid tool-name
shape) but with no namespace, so a client cannot invent namespaces in the store.

**How later phases add to it without a redesign.** Layer names, deny codes and upstream
statuses are checked by shape, not listed, so a new layer or code is new rows and no DDL.
Phase 3's approvals add an `approvals` table and a `dash_approvals` view; Phase 4's model calls
add a `model_usage` table (model, tokens, cost) and a view; both join on `request_id`. The
Phase 6 scorecard reads `layer_verdicts`: for each layer, verdicts with it on and off, joined
to the harness's own labels by client, tool, argument hash and time (or by a harness table that
joins on `request_id`; a `run_id` column is not added until that is decided). The dashboard tells
a layer that does not exist yet from one that exists and blocked nothing through
`dash_pipeline_layers`, which lists the layers of the configuration in use.

### How records reach Postgres

| Route | Trade-offs |
| --- | --- |
| **In-process: a bounded buffer and a background writer (used)** | No new service, one drop policy for records and spans, easy to test. The gateway carries `opentelemetry-sdk` and the writer. |
| OTLP to a Collector, then Postgres | The standard route, vendor neutral. A new container and configuration, and no mature Postgres trace exporter that I know of; decision records are not spans and would need their own path anyway. |
| OTLP to a receiver written for this | Isolates the writer, but is a new service, a new network hop and a new way to fail. |
| Log lines and a shipper | Simple, but gives up structure, and the shipper still has to write to Postgres. |

The route stays vendor neutral: the spans are ordinary OpenTelemetry, and a second processor
exporting OTLP can be added without touching the gateway's code.

The `PostgresEventSink` turns each event into rows and appends them to a `TelemetryBuffer`
(`telemetry/buffer.py`); a `BufferSpanProcessor` does the same for finished spans. A
`TelemetryWriter` task takes batches of up to 200 rows every second and inserts each batch in
one transaction as `telemetry_writer`, with `ON CONFLICT DO NOTHING` on every key, so a batch
retried after an ambiguous failure writes nothing twice.

### When writing fails

**Telemetry never blocks or fails a tool call.**

- The request path only appends to the buffer: a lock and an append, never a database call.
- The buffer is bounded (10 000 rows by default). When it is full the oldest row is dropped and
  counted, so a database that stays down costs memory up to the bound and no latency.
- A database that is down or silent keeps the current batch in the writer; connect, statement
  and total time are bounded, and it retries with backoff from 1 s to 30 s. The gateway starts
  and serves with the telemetry database unreachable.
- A row the database refuses (a CHECK) can never succeed, so it is not retried: the batch is
  split, the refused row is dropped and counted, and the rest are written. If the database fails
  during that split instead, the whole batch stays queued and the writer reports `degraded`;
  nothing is counted written or dropped that was not.
- Failed logins have a bounded quota of their own in the buffer and a tenth of every batch, so an
  unauthenticated peer who fails login at will can only evict other failed logins, and a steady
  stream of decision records cannot starve them.
- An extra sink that raises (the Postgres one) is logged, with the request id (the first failure in
  full, then at most a line a minute) and does not stop the next sink; the primary sink (the log,
  later the audit log) is not wrapped, so its failure reaches the pipeline and is logged with the
  request id as described above; a span that cannot be converted never raises into a request; the pipeline
  and the authentication middleware bound recording to 2 s whatever the sink does.
- On shutdown the writer makes one last, time-boxed attempt to flush.
- `/healthz` reports `telemetry: {status, queue_depth, dropped_total, rejected_total,
  written_total}` with `status` `ok`, `degraded` while writes fail, or `disabled`. It is never the
  reason the gateway is unhealthy: a restart would not help and tool calls do not depend on it.

Tests stop the database, make it hang, make it refuse a row, and cancel the writer, and check
that the request path is not slowed and that nothing is lost that should not be.

### Roles, and what the dashboard can see

| Role | Can | Cannot |
| --- | --- | --- |
| `telemetry_writer` (the gateway) | `INSERT` into the five tables | read, update or delete anything, in any schema |
| `telemetry_reader` (the dashboard) | `SELECT` four views: `dash_requests`, `dash_layer_verdicts`, `dash_auth_failures`, `dash_pipeline_layers`; at most 5 connections; session *defaults* of read-only, 5 s per statement and 10 s idle in a transaction | read a base table, the registry or any server's schema; write |
| `telemetry_purger` | `DELETE` from the four purgeable tables, and `SELECT` on their `ts` column only | read anything else, insert, update |

The views run with their owner's rights, so the reader needs no access to the tables, and they
leave out hashes, trace ids, lookup ids and the pipeline fingerprint. So the dashboard can see
counts, timings and names of clients, tools, layers and codes; it cannot see arguments or
results (they are not stored), tokens, scopes, addresses or any server's data. Privilege tests
assert each of these, and that a grant widened by hand, a role attribute added by hand
(`CREATEDB`, `CREATEROLE`) or a role membership granted by hand (`pg_read_all_data`) is taken
away again by the next `gateway-admin telemetry-setup`, which runs on every `docker compose up`
before the gateway.

Two limits of what is enforced, stated plainly:

- The reader's read-only, 5 s and 10 s settings are session **defaults**: Postgres lets a session
  change them, so a compromised dashboard could run an unbounded query. What binds is the grants
  and the five-connection limit. The dashboard (5c) sets the same timeouts on its own
  connections; a hard bound would need a watchdog or a connection pooler.
- Every role can run `lo_from_bytea` and the other large-object functions, because Postgres grants
  `EXECUTE` on them to PUBLIC. That lets the writer, purger or reader store data outside the
  schema, in the database's large-object store. Revoking it database-wide would change the other
  schemas' roles too, so it is left as it is; nothing in the gateway calls them.

### Retention

`telemetry-purge` runs hourly in its own service as the purger role. It deletes **spans after 7
days and everything else after 30** (`TELEMETRY_RETENTION_SPAN_DAYS`,
`TELEMETRY_RETENTION_DAYS`). Because the role can read only each table's time column it deletes in
windows of one hour (the oldest row comes from a B-tree), never a whole backlog in one statement. A pass that fails is retried at the
next interval. At demo volumes plain deletes are enough; at much larger volumes the tables would
be partitioned by day and old partitions dropped.

### Fresh trace at the gateway

The MCP SDK parents its server span under a `traceparent` the client sends in `_meta`, and the call
to an upstream carries whatever trace context is current, so a client used to be able to choose
which trace an upstream call joined. The gateway now starts `gateway.tool_call` and
`gateway.tools_list` in an empty context, so each request is a trace of its own and the upstream
receives only the gateway's trace id; a client's `tracestate` and `baggage` are not forwarded. A
test sends all three and checks the upstream and the stored spans.

### The traffic simulator

`scripts/simulate_traffic.py` sends a seeded, repeatable mix through the gateway as both bots with
no API key: normal calls (reads, and a few writes to the fictional ticketing data), calls the scope
layer refuses (a tool the bot was not granted, a tool that does not exist) and failed
authentications. The same seed gives the same counts; `--duration` spreads the run over time.
`--verify` reads the dashboard's views as the reader role and requires that what was stored equals
what was sent. CI runs it after the Harborline scenarios. It is what fills the dashboard for demos
and screenshots.

## Data model

| Table | Holds |
| --- | --- |
| `clients` | name, description, status (`active` or `disabled`) |
| `client_tokens` | lookup id, SHA-256, label, created, expires, revoked, last used |
| `client_scopes` | one row per granted exposed tool name |
| `upstream_servers` | namespace, URL, timeouts, the *name* of an environment variable holding its credential (never the value) |
| `tool_policies` | namespace, upstream tool name, `effect` (`read` or `write`), notes, when it was reviewed |

The `telemetry` schema is described under [Telemetry](#telemetry-phase-5), and the `policy` schema
(agent-core's `agent_core_audit` and `agent_core_approvals` tables) under [Audit](#audit-phase-3).

### Database roles

- **Owner:** the admin CLI and migrations run as the owner, from a separate `admin` Compose
  service.
- **`gateway_app`:** the gateway itself connects as `gateway_app`. It may read the
  registry and update `client_tokens.last_used_at`, and nothing else. It has no access to
  any server's schema.
- **`policy_gateway`, `policy_approver`, `policy_auditor`:** see
  [Roles, and the approval guard](#roles-and-the-approval-guard).
- **`telemetry_writer`, `telemetry_reader`, `telemetry_purger`:** see
  [Roles, and what the dashboard can see](#roles-and-what-the-dashboard-can-see).
- **`ticketing_app`, `crm_app`, `handbook_app`:** each server connects as its own role,
  described under [MCP servers](#mcp-servers-phase-2). None has access to the registry or to
  another server's schema.

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

## Dashboard (Phase 5c)

A read-only Next.js app in `dashboard/`, published on `127.0.0.1:4400` (standalone output, run by a
non-root user on a read-only filesystem). It shows what the gateway has recorded and changes nothing:
there is no approve, deny or revoke in it (approvals are decided with `gateway-approver`). Phase 5c-2
has the overview's KPIs, the recent decisions and the approval queue; the charts, the seeded demo
backfill and the memory measurement are 5c-3.

### What it can reach

It is on a network of its own with PostgreSQL (`dashboard`), not on `edge` with the gateway and not
on `backend`, so it has no route to the gateway or the MCP servers. A published port cannot come from
an internal network, so this one is an ordinary bridge with a route out; the dashboard holds only the
telemetry reader's credential and has no use for it. It reads through that role (`telemetry_reader`:
`SELECT` on the four telemetry views and `policy.dash_approvals`, five connections at most) and nothing
else: no base table, no registry, no arguments, no addresses.

Next.js's own telemetry is off (`NEXT_TELEMETRY_DISABLED=1` in every stage of the image and in the
service; a test pins both).

### The database, bounded by the dashboard

The reader role's read-only, 5 s and 10 s settings are session defaults a session can change, so the
dashboard sets its own: a pool of three connections that start `default_transaction_read_only=on` with
a statement timeout, and every query runs in `BEGIN READ ONLY` with `SET LOCAL statement_timeout`
(4 s). That bounds the dashboard's own queries; it is not a control against a compromised dashboard
process, which could reset the settings: what binds then is the role's grants and its five-connection
limit.

### Signing in

One admin, whose password is kept as a scrypt hash in `DASHBOARD_ADMIN_PASSWORD_HASH`
(`scrypt:<N>:<r>:<p>:<salt>:<hash>`, written without `$` so Compose does not read it as a variable).
Set it with `python3 scripts/set_dashboard_password.py`, which asks twice and prints nothing; empty
means nobody can sign in, and the sign-in page says so. scrypt uses 32 MiB a verification, so they are
done one at a time.

- **Session.** A signed cookie, `__Host-aig_session`: `HttpOnly; Secure; SameSite=Strict; Path=/`, no
  `Domain`. Its payload (a random id, when it was issued, when it was last seen) is signed with
  HMAC-SHA256 under `DASHBOARD_SESSION_SECRET`. It ends 30 minutes after it was last used and 8 hours
  after it was issued, however busy. The last-seen time moves when the person does something (opens or
  changes a page, pages the decisions), at most once a minute; the page's own 15-second refresh does not
  move it, so a tab left open ends 30 minutes after the last real use and its next refresh is turned
  away with a 401. The session itself is stored nowhere, so a restart signs nobody out and changing the
  secret signs everybody out. **Signing out is remembered in memory:** the session id goes on a list that
  the proxy and the pages check, so a copied cookie stops working at once; the list is lost on a restart,
  after which a cookie signed out before it is good again until it expires (at most 8 hours).
- **`Secure` is always set,** so the cookie is only kept where the browser treats the origin as secure.
  Chromium and Firefox do that for `localhost` and `127.0.0.1` over plain http; Safari does not for
  `http://127.0.0.1`, so there signing in appears to work and the next request is not signed in. Use
  Chromium or Firefox, or put TLS in front. (This is a known limitation, not something to turn off.)
- **Form posts** (sign in, sign out) must carry an `Origin` that names the host they were sent to, and
  `Sec-Fetch-Site` of `same-origin` where the browser sends it; with `SameSite=Strict` that is the second
  defence against a cross-site post.
- **Failed sign-ins** are counted for the whole dashboard (behind Docker's port publishing every client
  arrives from the bridge's address, so a per-address count would be one count anyway). Five attempts are
  allowed; the fifth starts a one-minute lockout in which the password is not looked at, and each attempt
  after a lockout starts one twice as long, up to 15 minutes. So a guesser gets five tries and then one
  per lockout, about 96 a day, however long they wait: the count does not expire, only a success (or a
  restart) clears it, because a window that forgets lets the guesser have a fresh burst once the
  lockouts outgrow it. An attempt is counted when it starts, so guesses sent at once are counted too. The
  real admin, locked out by someone else's guesses, waits at most 15 minutes. Every failure is the same
  "did not match". The password is also limited to 12 to 512 characters.

### Headers and the proxy

`src/proxy.ts` runs before every request that is not a static file. It puts a fresh nonce
Content-Security-Policy on every answer (`default-src 'none'`; scripts only `'self'` with the nonce and
`'strict-dynamic'`; styles from `'self'` with the nonce and **no inline style attributes at all**, so
there is no `style=` anywhere in the UI; `connect-src`, `img-src`, `font-src` `'self'`; `form-action`
`'self'`; `frame-ancestors`, `base-uri` and `object-src` none) with `X-Content-Type-Options`,
`Referrer-Policy: same-origin` (not `no-referrer`: a browser sends `Origin: null` on a form POST under
`no-referrer`, and the same-origin check on sign-in would refuse the real form), `X-Frame-Options`, `Cross-Origin-Opener-Policy` and
`Cross-Origin-Resource-Policy`, a `Permissions-Policy` that turns everything off, and
`Cache-Control: no-store`. It sends anyone without a valid session to `/signin` (an API call gets 401);
the sign-in page, its form and `/healthz` (which reads nothing) are open. It is the first gate, not the
only one: the pages check the session, and so does every data function.

Three details that matter. The proxy does not run on static files, so `next.config.ts` gives `/_next/static`,
`/brand` and `/fonts` `X-Content-Type-Options` and `Cross-Origin-Resource-Policy` itself. Next's built-in
404 and last-resort error pages use inline styles and un-nonced scripts, which this CSP would block, so
the app has its own `not-found.tsx` and `global-error.tsx` made of the system's classes. And Next
buffers a request body for the proxy before the proxy looks at the request, so the limit is set to
16 KB (`proxyClientMaxBodySize`): the only body the dashboard takes is a password form. Every request
is also refused with 421 unless its `Host` is in `DASHBOARD_ALLOWED_HOSTS` (loopback by default), which
is what stops DNS rebinding: the `Origin` check alone compares `Host` with a value an attacker also
controls.

### Every data function takes a session

`src/lib/data` exports three functions (`getKpis`, `getDecisions`, `getApprovals`), each with an
`AuthedSession` first. That type can only be made by the code that has checked the signed cookie
(`mint`), and each function, and the database runner under them, checks it again before anything is
read. A test loads every export of the module and requires each to reject a missing, empty, forged or
wrong-typed session before the database is touched, so a function added without the check fails it.

### Panels and refresh

Each panel has loading, empty, error and (for tokens and cost, which start with the injection
classifier in Phase 4) not-active states, and status is always an icon and words, never colour alone.
The page is rendered on the server with the data, then refreshed every 15 seconds from `/api/live`
while the tab is visible: it stops when the tab is hidden and refreshes at once on return after a gap,
and a failed refresh keeps the last good data on screen with its age. The decisions are paged by
keyset (time to the microsecond, then request id), so a row written while someone pages is neither
skipped nor shown twice; a cursor comes back from the browser and is checked before it is used. Times
are UTC.

### Charts, the demo stack and the memory limit (5c-3)

Five charts sit between the KPIs and the decisions: request volume (forwarded and blocked), latency
(p50, p95, p99), success and errors by tool, blocked by layer (blocked, and would-block in monitor
mode) and failed sign-ins by reason with a trend. They are server-rendered SVG with no chart library,
in the UI system's chart tokens. Each has a plain-words summary and its numbers as a table, and every
status is an icon and a word, never colour alone. When one spike would flatten the latency lines, the
axis stops at a labelled ceiling; the table and summary keep the true peak.

`scripts/run_dashboard_demo.sh all` brings up a separate Compose project (`ai-gateway-demo`, never a
real stack's name) with `compose.demo.yaml`, seeds about a week of fictional Harborline Supply Co.
telemetry with `scripts/seed_dashboard_demo.py` (a daily rhythm and four attack episodes; the same seed
gives the same counts), puts four approvals pending and one each approved, rejected and expired through
the real path with two demo approver logins, and takes the screenshots in `docs/images/` with
`scripts/screenshots.py`. The demo admin password is made for the run and kept only in a git-ignored
`.demo/`; `DASHBOARD_DEMO=1` pins "now" to the newest seeded row so the week is always in range.

The memory limit is measured, not guessed: `scripts/measure_dashboard_memory.py` samples the container
once a second through a first load, ten concurrent loads in both themes, the 7- and 30-day ranges and a
30-attempt sign-in burst, three rounds. The highest sampled value was 76 MiB and the cgroup's own peak
94 MiB, so the limit is 144 MiB (1.5 times 94) and the Node heap 86 MiB (60%). The Compose total stays
under 3072 MiB.

### What is not here

Sign-out revocation and the sign-in throttle are held in memory, so a restart forgets them. The
dashboard's fonts (IBM Plex Sans and Mono, Space Grotesk) are self-hosted under the SIL Open Font
License 1.1; see `THIRD_PARTY_NOTICES.md`.

## Health check

`GET /healthz` needs no token and, like everything else, is published on 127.0.0.1 only.
The gateway answers 200 while the MCP endpoint is serving and 503 otherwise. The three
servers (4410, 4411, 4412, reachable only inside the Compose network) use the same format
and the same cache (`mcp_common.health`) and answer 200 while they can read their own schema version, 503 with `status: "unavailable"` when it cannot.
Their `GIT_COMMIT`/`GIT_BRANCH` come from the build arguments of `servers/Dockerfile`, a
server's `version` is its own package (`ticketing-server`, `crm-server`, `handbook-server`),
and `schema_version` is the newest migration in its schema (its role may read that
schema's `schema_migrations`; for CRM and handbook that is all it can read of the table
itself).

| Field | Meaning |
| --- | --- |
| `status` | `ok` or `unavailable` |
| `commit` | The commit the image was built from, set by the `GIT_COMMIT` build argument; `null` when not given |
| `commit_source` | `process_start`: the commit is fixed for the life of the process |
| `branch` | Set by the `GIT_BRANCH` build argument; `null` when not given |
| `version` | The `ai-gateway` package version |
| `schema_version` | The newest applied migration of the registry, zero-padded (`"0004"`). Re-read at most every 30 s by one caller at a time, with the read bounded to 2 s; `null` if it cannot be read in time |
| `uptime_s` | Seconds since the process started |
| `audit` | The gateway only: `{status, queue_depth, dropped_total, rejected_total, written_total}`; `status` is `ok`, `degraded` while writes fail, or `disabled` (no audit database). It never makes the gateway unhealthy. |
| `telemetry` | The gateway only: `{status, queue_depth, dropped_total, rejected_total, written_total}`; `status` is `ok`, `degraded` while writes fail, or `disabled`. It never makes the gateway unhealthy. |

## Ports

| Service | Address |
| --- | --- |
| Dashboard (Phase 5c) | 127.0.0.1:4400 |
| Gateway | 127.0.0.1:4401 |
| PostgreSQL | 127.0.0.1:4402 |
| MCP servers (Phase 2) | none on the host; 4410 (handbook), 4411 (CRM) and 4412 (ticketing) inside the `backend` network |

The test-only echo server has no published port either.

## Networks

Compose has three networks:

| Network | Kind | Members |
| --- | --- | --- |
| `edge` | ordinary bridge | the gateway and PostgreSQL, the two services that publish a port |
| `dashboard` | ordinary bridge | the dashboard and PostgreSQL, and nothing else |
| `backend` | `internal: true` | the three MCP servers, `servers-setup`, `telemetry-setup`, `telemetry-purge`, `policy-setup`, `migrate`, `admin`, `direct-check`, the test upstream, and also the gateway and PostgreSQL |

Docker gives an internal network no route to the outside world and publishes no port from
it. That alone is not enough: Docker filters traffic that is forwarded off the network, not
traffic addressed to the host itself, so a container on an internal bridge can still connect
to any host service bound to `0.0.0.0` (SSH, a proxy someone starts later) at the bridge's own
address. `backend` therefore also sets `com.docker.network.bridge.inhibit_ipv4: "true"`,
which gives the host no address on it. So the MCP servers, which only need the database and
the gateway, **cannot reach the Internet or the host's services**, and neither can anything
else on `backend` alone: a server that a prompt injection or a bug turns against its operator
has nowhere to send what it read. It also makes the handbook's "no network at run time"
claim true by construction instead of by reading the code. The gateway and PostgreSQL are on
both networks, because the host reaches them and they reach the servers. PostgreSQL is also on `dashboard`.

A server therefore publishes no host port, and the host cannot call it, not even by the
container's address. Anything that needs to call a server directly runs inside the network:

- `scripts/direct_check.sh` runs the Harborline scenario against each server from the
  `direct-check` service.
- `scripts/check_servers_have_no_internet.sh` is the proof, and CI runs it. From inside each
  server it checks that PostgreSQL answers, that two public addresses cannot be connected to,
  that an outside name does not resolve, that a throwaway listener on the host bound to
  `0.0.0.0` cannot be reached at any of the host's addresses (the bridge's included), and that
  the server publishes no host port. The same probe in the gateway container is the control:
  it must reach the Internet and the listener, so a pass shows the probe can tell a blocked
  route from an open one. Without `inhibit_ipv4` the listener check fails.
- `gateway/tests/test_compose.py` asserts the layout in `compose.yaml`: the network is
  internal and gives the host no address, each internal-only service names it and nothing
  else, none publishes a port, only the gateway and PostgreSQL are on `edge`, and every
  published port is bound to `127.0.0.1`.

One path out remains and is accepted: PostgreSQL is on `edge`, and the owner role that
`servers-setup`, `migrate` and `admin` use is a superuser, which can make the database server
open outbound connections (for example with `COPY ... TO PROGRAM`). Those one-shots hold that
credential and run only code in this repository; the three servers never hold it, they use
their own least-privilege roles.

A new service joins `backend` only unless it must be reached from the host or needs the
Internet; a service that names no network would join Compose's default one, which has a route
out.
