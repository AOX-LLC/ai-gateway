import { type AuthedSession, assertAuthed } from "../auth/authed";
import { readOnly } from "../db";
import { nowSql } from "./clock";
import { RANGES, type Range } from "./ranges";
import type { Kpis } from "./types";

const kpisSql = (): string => `
WITH calls AS (
  SELECT request_id, outcome, upstream_status, duration_ms, deny_code
  FROM telemetry.dash_requests
  WHERE kind = 'tool_call' AND ts >= ${nowSql()} - make_interval(secs => $1::int)
)
SELECT
  (SELECT count(*) FROM calls) AS requests,
  (SELECT count(*) FROM calls WHERE outcome = 'forwarded') AS forwarded,
  (SELECT count(*) FROM calls WHERE outcome = 'forwarded' AND upstream_status = 'ok') AS succeeded,
  (SELECT count(*) FROM calls WHERE outcome = 'blocked') AS blocked,
  (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) FROM calls
     WHERE deny_code IS DISTINCT FROM 'approval_pending') AS p95_ms,
  (SELECT count(DISTINCT request_id) FROM telemetry.dash_layer_verdicts
     WHERE verdict = 'would_block' AND ts >= ${nowSql()} - make_interval(secs => $1::int)) AS would_block,
  (SELECT count(*) FROM telemetry.dash_auth_failures
     WHERE ts >= ${nowSql()} - make_interval(secs => $1::int)) AS auth_failures
`;

export async function getKpis(session: AuthedSession, range: Range): Promise<Kpis> {
  assertAuthed(session);
  const seconds = RANGES[range];
  const { rows } = await readOnly(session, (client) => client.query(kpisSql(), [seconds]));
  const row = rows[0] as Record<string, string | number | null>;
  const count = (key: string) => Number(row[key] ?? 0);
  const forwarded = count("forwarded");
  const succeeded = count("succeeded");
  const requests = count("requests");
  return {
    windowSeconds: seconds,
    requests,
    perMinute: requests / (seconds / 60),
    p95Ms: row.p95_ms === null || row.p95_ms === undefined ? null : Number(row.p95_ms),
    forwarded,
    succeeded,
    successRate: forwarded === 0 ? null : succeeded / forwarded,
    blocked: count("blocked"),
    wouldBlock: count("would_block"),
    authFailures: count("auth_failures"),
  };
}
