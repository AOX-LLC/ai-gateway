import { type AuthedSession, assertAuthed } from "../auth/authed";
import { readOnly } from "../db";
import { nowSql } from "./clock";
import { type Cursor, decodeCursor, encodeCursor } from "./cursor";
import { RANGES, type Range } from "./ranges";
import type { Decision, DecisionsPage } from "./types";

export const DEFAULT_PAGE_SIZE = 25;
export const MAX_PAGE_SIZE = 50;

// ts as text keeps its microseconds: a JavaScript Date would round them away, and the cursor compares
// them. `r` is the view, so only its columns are ever selected.
const pageSql = (): string => `
SELECT r.request_id,
       to_char(r.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS ts,
       r.client_name, r.tool, r.namespace, r.effect, r.outcome, r.blocked_by, r.deny_code,
       r.upstream_status, r.duration_ms,
       COALESCE((SELECT array_agg(DISTINCT v.layer ORDER BY v.layer) FROM telemetry.dash_layer_verdicts v
                  WHERE v.request_id = r.request_id AND v.verdict = 'would_block'), '{}') AS would_block
FROM telemetry.dash_requests r
WHERE r.kind = 'tool_call'
  AND r.ts >= ${nowSql()} - make_interval(secs => $1::int)
  AND ($2::timestamptz IS NULL OR (r.ts, r.request_id) < ($2::timestamptz, $3::uuid))
ORDER BY r.ts DESC, r.request_id DESC
LIMIT $4
`;

export type DecisionsQuery = { range: Range; cursor?: string | null; limit?: number };

export async function getDecisions(session: AuthedSession, query: DecisionsQuery): Promise<DecisionsPage> {
  assertAuthed(session);
  const limit = Math.min(Math.max(Math.trunc(query.limit ?? DEFAULT_PAGE_SIZE), 1), MAX_PAGE_SIZE);
  const cursor: Cursor | undefined = decodeCursor(query.cursor);
  const { rows } = await readOnly(session, (client) =>
    client.query(pageSql(), [RANGES[query.range], cursor?.ts ?? null, cursor?.id ?? null, limit + 1]),
  );
  const page = rows.slice(0, limit).map(toDecision);
  const last = page.at(-1);
  const more = rows.length > limit && last !== undefined;
  return { decisions: page, nextCursor: more ? encodeCursor({ ts: last.ts, id: last.requestId }) : null };
}

function toDecision(row: Record<string, unknown>): Decision {
  const text = (key: string) => (row[key] === null || row[key] === undefined || row[key] === "" ? null : String(row[key]));
  return {
    requestId: String(row.request_id),
    ts: String(row.ts),
    clientName: text("client_name") ?? "unknown",
    tool: text("tool") ?? "unknown",
    namespace: text("namespace"),
    effect: text("effect"),
    outcome: row.outcome === "blocked" ? "blocked" : "forwarded",
    blockedBy: text("blocked_by"),
    denyCode: text("deny_code"),
    upstreamStatus: text("upstream_status"),
    durationMs: row.duration_ms === null || row.duration_ms === undefined ? null : Number(row.duration_ms),
    wouldBlock: Array.isArray(row.would_block) ? row.would_block.map(String) : [],
  };
}
