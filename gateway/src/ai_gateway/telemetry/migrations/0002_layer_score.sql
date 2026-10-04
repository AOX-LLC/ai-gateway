-- A layer's own count for a verdict (matches found, violations): an integer, never content. It
-- shows in monitor mode how close allowed calls come to a layer's limit, and the scorecard reads it.
ALTER TABLE layer_verdicts ADD COLUMN score integer CHECK (score >= 0);

-- Columns are only ever added at the end of a view.
CREATE OR REPLACE VIEW dash_layer_verdicts AS
SELECT request_id, ts, layer, hook, mode, verdict, code, duration_ms, score
FROM layer_verdicts;
