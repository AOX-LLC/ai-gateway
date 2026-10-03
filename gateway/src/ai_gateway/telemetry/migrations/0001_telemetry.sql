-- Harborline Supply Co. is fictional; so is everything recorded here.
--
-- Spans and decision records of the gateway. The tables have no column that could hold a tool
-- argument, a tool result, a credential or a client address: only ids, hashes, codes, names
-- of clients, tools and layers, and timings. Free text is bounded by a CHECK, so even a bug
-- in the writer cannot put a payload in. Grants are applied by `gateway-admin telemetry-setup`
-- on every run, not here.
--
-- Layer names, codes and statuses are checked by shape, not listed, so a later phase can add
-- a layer, a deny code or an upstream status without a migration.

-- One row per decision record: a tools/call or a tools/list.
CREATE TABLE requests (
    request_id             uuid        PRIMARY KEY,
    ts                     timestamptz NOT NULL,
    kind                   text        NOT NULL CHECK (kind IN ('tool_call', 'tools_list')),
    client_id              uuid,
    client_name            text        CHECK (char_length(client_name) <= 100),
    tool                   text        CHECK (char_length(tool) <= 64),
    namespace              text        CHECK (namespace ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    effect                 text        CHECK (effect IN ('read', 'write')),
    effect_source          text        CHECK (effect_source IN ('policy', 'default')),
    outcome                text        NOT NULL CHECK (outcome IN ('forwarded', 'blocked', 'listed')),
    blocked_by             text        CHECK (blocked_by ~ '^[a-z][a-z0-9_]{0,62}$'),
    deny_code              text        CHECK (deny_code ~ '^[a-z][a-z0-9_]{0,62}$'),
    upstream_status        text        CHECK (upstream_status ~ '^[a-z][a-z0-9_]{0,31}$'),
    duration_ms            real        NOT NULL CHECK (duration_ms >= 0),
    upstream_duration_ms   real        CHECK (upstream_duration_ms >= 0),
    args_sha256            text        CHECK (args_sha256 ~ '^[0-9a-f]{64}$'),
    protocol_version       text        CHECK (protocol_version ~ '^[0-9A-Za-z._-]{1,20}$'),
    pipeline_config_sha256 text        CHECK (pipeline_config_sha256 ~ '^[0-9a-f]{64}$'),
    trace_id               text        CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    tools_available        integer     CHECK (tools_available >= 0),
    tools_returned         integer     CHECK (tools_returned >= 0)
);

-- Newest first, for the recent-decisions list (keyset pages on ts, request_id).
CREATE INDEX requests_by_time ON requests (ts DESC, request_id DESC);
CREATE INDEX requests_by_client ON requests (client_name, ts DESC);
CREATE INDEX requests_by_tool ON requests (tool, ts DESC);
-- Blocked attempts are a small share of the rows and the question asked most often.
CREATE INDEX requests_blocked ON requests (ts DESC) WHERE outcome = 'blocked';

-- Every layer's verdict on a request. The Phase 6 scorecard reads attack success per layer
-- from here, with the layer on and off.
CREATE TABLE layer_verdicts (
    request_id  uuid        NOT NULL,
    ordinal     smallint    NOT NULL,
    ts          timestamptz NOT NULL,
    layer       text        NOT NULL CHECK (layer ~ '^[a-z][a-z0-9_]{0,62}$'),
    hook        text        NOT NULL CHECK (hook IN ('filter', 'before_call', 'after_call')),
    mode        text        NOT NULL CHECK (mode IN ('enforce', 'monitor', 'off')),
    verdict     text        NOT NULL CHECK (verdict IN ('allow', 'deny', 'would_block', 'off', 'error')),
    code        text        CHECK (code ~ '^[a-z][a-z0-9_]{0,62}$'),
    tools_removed integer   CHECK (tools_removed >= 0),
    duration_ms real        CHECK (duration_ms >= 0),
    PRIMARY KEY (request_id, ordinal)
);

CREATE INDEX layer_verdicts_by_layer ON layer_verdicts (layer, verdict, ts DESC);
-- A B-tree, not BRIN: the purge asks for the oldest row, and a B-tree answers that without a scan.
CREATE INDEX layer_verdicts_by_time ON layer_verdicts (ts);

-- Failed authentication. The lookup id is the non-secret part of a token; the address of the
-- peer is not stored (behind Docker's port publishing it is only the bridge).
CREATE TABLE auth_failures (
    event_id  uuid        PRIMARY KEY,
    ts        timestamptz NOT NULL,
    reason    text        NOT NULL CHECK (reason ~ '^[a-z][a-z0-9_]{0,31}$'),
    lookup_id text        CHECK (lookup_id ~ '^[a-z2-7]{8}$')
);

CREATE INDEX auth_failures_by_time ON auth_failures (ts DESC);
CREATE INDEX auth_failures_by_reason ON auth_failures (reason, ts DESC);

-- OpenTelemetry spans of the gateway's own instrumentation. `ts` is the start time. The
-- exporter keeps an allowlist of attribute keys; the size check is the backstop.
CREATE TABLE spans (
    trace_id       text        NOT NULL CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    span_id        text        NOT NULL CHECK (span_id ~ '^[0-9a-f]{16}$'),
    parent_span_id text        CHECK (parent_span_id ~ '^[0-9a-f]{16}$'),
    ts             timestamptz NOT NULL,
    name           text        NOT NULL CHECK (name ~ '^[a-z][a-z0-9_.]{0,99}$'),
    duration_us    bigint      NOT NULL CHECK (duration_us >= 0),
    status         text        NOT NULL CHECK (status IN ('unset', 'ok', 'error')),
    request_id     uuid,
    attrs          jsonb       NOT NULL DEFAULT '{}' CHECK (octet_length(attrs::text) <= 2048),
    PRIMARY KEY (trace_id, span_id)
);

CREATE INDEX spans_by_time ON spans (ts);
CREATE INDEX spans_by_request ON spans (request_id) WHERE request_id IS NOT NULL;

-- The pipeline's layers and their modes, once per configuration fingerprint. It tells the
-- dashboard a layer that does not exist yet from one that exists and has blocked nothing.
CREATE TABLE pipeline_configs (
    sha256     text        PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    first_seen timestamptz NOT NULL,
    layers     jsonb       NOT NULL CHECK (octet_length(layers::text) <= 4096)
);

-- What the dashboard may read. The views run with their owner's rights, so the dashboard's
-- role needs no access to the tables. They leave out hashes, trace ids and lookup ids.
CREATE VIEW dash_requests AS
SELECT request_id, ts, kind, client_name, tool, namespace, effect, outcome, blocked_by,
       deny_code, upstream_status, duration_ms, upstream_duration_ms
FROM requests;

CREATE VIEW dash_layer_verdicts AS
SELECT request_id, ts, layer, hook, mode, verdict, code, duration_ms
FROM layer_verdicts;

CREATE VIEW dash_auth_failures AS
SELECT ts, reason
FROM auth_failures;

-- The layers of the configuration in use, in pipeline order: the one the newest request ran
-- under, so a rollback to an older configuration shows correctly, or, before any request, the
-- one first seen last.
CREATE VIEW dash_pipeline_layers AS
SELECT item.position::integer AS position,
       item.layer ->> 'name'  AS layer,
       item.layer ->> 'mode'  AS mode,
       latest.first_seen
FROM (
    SELECT layers, first_seen
    FROM pipeline_configs
    ORDER BY sha256 = (SELECT pipeline_config_sha256 FROM requests
                       WHERE pipeline_config_sha256 IS NOT NULL
                       ORDER BY ts DESC LIMIT 1) DESC NULLS LAST,
             first_seen DESC
    LIMIT 1
) AS latest,
     jsonb_array_elements(latest.layers) WITH ORDINALITY AS item(layer, position);
