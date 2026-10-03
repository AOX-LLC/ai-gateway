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
- **Blocked by any other layer:** `-32010`, `Request blocked by gateway policy.`, with a
  request id. The layer name appears only in the decision record.
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

The seams that the shared `agent-core` library will fill (approval queue, audit log,
tracing) follow its **pre-release, unmerged interfaces**. Those may shift before its v0.1.0
release, so expect small adapter changes when it lands. This repository does not import
it or copy its code.

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
schema. agent-core is pinned by tag (`v0.1.0a2`, in `gateway/pyproject.toml`); the log and, from
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

**Cost.** agent-core appends one event per transaction and serialises appends: about 22 ms each
(p95 about 100 ms) when measured here, so 50 concurrent appends took 3.2 s. The recorder therefore
writes the queue in batches of up to 100 in one transaction, using `SQLAuditLog.append_in`: 100
events took about 150 ms. Only the write-ahead record waits for its own transaction.

**Bounded waits.** agent-core runs each operation on the event loop's default thread pool, which
also resolves names for psycopg and httpx, and a worker thread cannot be interrupted. Any role
that can connect can hold an advisory lock, so the gateway keeps the audit log off that pool, on
workers of its own (four for the write-ahead record, one for the batches), and counts the threads
still running, including one whose caller gave up. A caller stops waiting at its limit (2 s for
the write-ahead record, 15 s for a batch); the thread ends when the database's own limits end its
transaction: a 2 s connect timeout, then lock, statement and transaction timeouts (1.5, 1.8 and
1.9 s for the write-ahead record; 5, 8 and 12 s for the batches; the transaction timeout needs
PostgreSQL 17). A write is refused at once when all four write-ahead workers are taken. This
narrows the chance that a write refused for time is committed late; it does not remove it, since
a thread that connects just before the limit can still commit after the request has moved on.
A batch whose outcome is unknown (a timeout, or a connection lost after COMMIT) is checked against
the records written since the last one known, by `record_id`, before it is retried, and its gap
record is built once, so a retry does not store either twice.

### Roles, and the approval guard

| Role | Can |
| --- | --- |
| `policy_gateway` (the gateway) | read and append the audit log; create approval requests (pending only) and consume an approved one |
| `policy_approver` (a person's tool, `gateway-approver`) | read and append the audit log (it writes the audit event of a decision); decide a pending request; read the approvers and the stored arguments |
| `policy_auditor` | read the audit log, and nothing else (never the arguments) |

The gateway can insert a request's arguments (`approval_arguments`) and never read them back; only
the approver reads them.

agent-core's installer gives one app role `UPDATE` on approvals, and the rule that only a human
resolves one lives in library code. That role could therefore approve its own requests with plain
SQL (confirmed). A guard trigger closes that:
- the gateway role may only move an *approved*, unexpired request to *consumed*, and change nothing
  else about it;
- the approver may only decide a *pending*, unexpired request, with a decision that matches its
  status and with who and when filled in, and change nothing about what it authorises, who asked or
  when it expires;
- nobody may create a request that is already decided;
- any other role, including one that merely inherits a policy role's privileges (the guard keys on
  the member's own name), is refused, and setup removes every membership in the policy roles. The gateway cannot decide an approval by any route,
whatever its code does. `gateway-admin policy-setup` installs agent-core's tables once, for a
scratch role that cannot log in (so no real role holds a grant before its guard exists), applies the
grants and the guard in one transaction, guard first, and is safe to repeat and to run twice at
once, within one database (the scratch role is named per database, and any left by a killed setup is
dropped by the next run). The audit table's own protections are agent-core's: triggers that refuse
`UPDATE`, `DELETE` and `TRUNCATE`, and a check that the connecting role cannot do any of them. The
database owner can disable the triggers, which is what the anchor test does; the chain and an
anchor are what show it.

### Approvals (Phase 3b)

A write waits for a person. The `approval` layer runs last, so a person approves exactly the call
that is forwarded; a read never asks.

1. The gateway looks for this client's newest unexpired request for this tool and these arguments
   (the hash of `{tool, arguments}`). If there is none it submits one (30 minutes to live,
   `settings.approval_ttl_s`) and stores the arguments for the approver.
2. It **holds** the call for up to 45 s (`approval_hold_s`, polling once a second: agent-core has
   no wait/notify) for a decision. At most 16 calls are held at once; past that a call is answered
   "pending" at once.
3. **Approved:** the approval is consumed, once, and the call goes on to the audit write-ahead and
   the upstream. agent-core's consume checks the tool and the arguments but not who asks, so the
   gateway first checks that the request was made by *this* client. One approval authorises one run.
4. **No decision yet:** the client gets the pending result and retries the same call; the retry
   finds the same request and holds again. **Rejected:** the rejection stands until the request
   expires, so a retry does not ask again. **Expired:** the next call asks afresh.
5. **The queue cannot be used** (no policy database, a dead database, arguments over 64 KiB):
   the write is refused. The layer fails closed.

An approval is for exactly one tool with exactly those arguments for the client that asked: other
arguments are a new request; another client's request is never found, and a direct attempt to use
it is refused.

**The arguments are kept, once.** So that a person approves what the call really says, the full
arguments of a write awaiting approval are stored in `policy.approval_arguments`. It is the one
exception to "never store arguments". The gateway role can insert them and cannot read them; the
approver role reads them; the auditor and the dashboard's reader cannot. They are never in an audit
record or in telemetry, and `policy.purge_approval_arguments()` (run hourly by the gateway; only the
gateway role may call it) deletes them 7 days after they were stored.

**`gateway-approver`** runs as the approver role (`docker compose run --rm approver --as <id>
list | show | approve | reject`). `--as` names a person registered with `gateway-admin
approver-add`, who must be active and hold the request's role (`approver`). The database role is
shared, so `--as` records who decided; it does not prove it. The dashboard and the lab approver of
later phases authenticate people themselves. Before it shows a request as approvable, the tool
parses the arguments back from the text it is about to display and hashes them with the tool name:
the hash must be the one stored when the gateway asked, or it refuses (a request whose arguments
were changed, or purged, can only be rejected). The arguments are shown as JSON with every
non-ASCII and control character escaped, and anything from outside (names, summaries) has control
characters and ANSI sequences removed, so nothing in a call can redraw the approver's screen.

`policy.dash_approvals` is the dashboard's view of the requests (tool, client, status, who decided,
times) with no arguments and no free text; the dashboard's reader role is granted that view and
nothing else in the schema.

What is *not* here: a person's identity is not authenticated by the CLI (above), and the approval
is consumed before the audit write-ahead, so a write refused because the audit log is down has used
up its approval (the client asks again). `scripts/auto_approver.py` approves for the scenario and
the simulator; it is test tooling, outside the gateway, and needs `--approve-as` and
`LAB_AUTO_APPROVE=yes`.

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
| `layer_verdicts` | request × layer × hook | the layer's name, the hook (`filter`, `before_call`, `after_call`), its mode, the verdict (`allow`, `deny`, `would_block`, `off`, `error`), code, time |
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
| Dashboard (Phase 5) | 127.0.0.1:4400 |
| Gateway | 127.0.0.1:4401 |
| PostgreSQL | 127.0.0.1:4402 |
| MCP servers (Phase 2) | none on the host; 4410 (handbook), 4411 (CRM) and 4412 (ticketing) inside the `backend` network |

The test-only echo server has no published port either.

## Networks

Compose has two networks:

| Network | Kind | Members |
| --- | --- | --- |
| `edge` | ordinary bridge | the gateway and PostgreSQL, the two services that publish a port |
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
both networks, because the host reaches them and they reach the servers.

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
