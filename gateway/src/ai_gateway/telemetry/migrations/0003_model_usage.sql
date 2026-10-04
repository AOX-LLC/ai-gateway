-- What the model calls cost. One row per call the gateway made to a model (the injection
-- classifier's judgements), joined to its request by `request_id`: the model, the tokens, the cost
-- and the time, and whether the call was `live` (billed), `record` (billed, for a recording) or
-- `replay` (served from a committed recording: the cost is the recording's, and nothing was
-- billed). Never the text that was judged, and never what was answered.
CREATE TABLE model_usage (
    usage_id     uuid        PRIMARY KEY,
    request_id   uuid,
    ts           timestamptz NOT NULL,
    layer        text        NOT NULL CHECK (layer ~ '^[a-z][a-z0-9_]{0,62}$'),
    purpose      text        NOT NULL CHECK (purpose IN ('tool_result', 'tool_arguments')),
    model        text        NOT NULL CHECK (length(model) BETWEEN 1 AND 100),
    tier         text        NOT NULL CHECK (tier IN ('small', 'mid', 'large')),
    mode         text        NOT NULL CHECK (mode IN ('replay', 'record', 'live')),
    input_tokens integer     NOT NULL CHECK (input_tokens >= 0),
    output_tokens integer    NOT NULL CHECK (output_tokens >= 0),
    cache_read_tokens integer NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens integer NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cost_usd     numeric(14, 8) NOT NULL CHECK (cost_usd >= 0),
    latency_ms   real        NOT NULL CHECK (latency_ms >= 0),
    status       text        NOT NULL CHECK (status IN ('ok', 'unrecorded', 'error'))
);

CREATE INDEX model_usage_by_time ON model_usage (ts);
CREATE INDEX model_usage_by_request ON model_usage (request_id);

-- A verdict a layer could not give: replay mode had no recording for the text. Never `allow`.
ALTER TABLE layer_verdicts DROP CONSTRAINT layer_verdicts_verdict_check;
ALTER TABLE layer_verdicts ADD CONSTRAINT layer_verdicts_verdict_check
    CHECK (verdict IN ('allow', 'deny', 'would_block', 'off', 'error', 'unclassified'));

CREATE VIEW dash_model_usage AS
SELECT ts, layer, purpose, model, tier, mode, input_tokens, output_tokens,
       cache_read_tokens, cache_write_tokens, cost_usd, latency_ms, status
FROM model_usage;
