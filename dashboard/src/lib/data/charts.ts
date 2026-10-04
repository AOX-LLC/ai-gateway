import { type AuthedSession, assertAuthed } from "../auth/authed";
import { readOnly } from "../db";
import { nowSql } from "./clock";
import { BUCKET_SECONDS, RANGES, type Range } from "./ranges";
import type { Charts, LatencyPoint, LayerCount, ReasonCount, ToolOutcome, TrendPoint, VolumePoint } from "./types";

// Bucket edges come from date_bin on a fixed origin that is a whole number of days after 1970, so
// they are the same instants as flooring the epoch seconds, which is how the gaps are filled below.
const ORIGIN = "TIMESTAMPTZ '2000-01-01 00:00:00+00'";
const WINDOW = (): string => `ts >= ${nowSql()} - make_interval(secs => $1::int)`;
const BIN = `date_bin(make_interval(secs => $2::int), ts, ${ORIGIN})`;
const EPOCH = (expression: string): string => `extract(epoch FROM ${expression})::bigint`;

// Calls held for approval last as long as the hold (tens of seconds): that is waiting for a person,
// not latency, so they stay out of the percentiles (they are still in the volume and the outcomes).
const TIME_SERIES = (): string => `
SELECT ${EPOCH(BIN)} AS bucket,
       count(*) FILTER (WHERE outcome = 'forwarded') AS forwarded,
       count(*) FILTER (WHERE outcome = 'blocked') AS blocked,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
         FILTER (WHERE deny_code IS DISTINCT FROM 'approval_pending') AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
         FILTER (WHERE deny_code IS DISTINCT FROM 'approval_pending') AS p95,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms)
         FILTER (WHERE deny_code IS DISTINCT FROM 'approval_pending') AS p99
FROM telemetry.dash_requests
WHERE kind = 'tool_call' AND ${WINDOW()}
GROUP BY 1 ORDER BY 1`;

const TOOLS = (): string => `
SELECT tool,
       count(*) FILTER (WHERE upstream_status = 'ok') AS ok,
       count(*) FILTER (WHERE upstream_status IS DISTINCT FROM 'ok') AS error
FROM telemetry.dash_requests
WHERE kind = 'tool_call' AND outcome = 'forwarded' AND tool IS NOT NULL AND ${WINDOW()}
GROUP BY tool ORDER BY count(*) DESC, tool LIMIT 8`;

const BLOCKED_BY_LAYER = (): string => `
SELECT blocked_by AS layer, count(*) AS blocked
FROM telemetry.dash_requests
WHERE kind = 'tool_call' AND outcome = 'blocked' AND blocked_by IS NOT NULL AND ${WINDOW()}
GROUP BY blocked_by`;

const WOULD_BLOCK_BY_LAYER = (): string => `
SELECT layer, count(DISTINCT request_id) AS would_block
FROM telemetry.dash_layer_verdicts
WHERE verdict = 'would_block' AND ${WINDOW()}
GROUP BY layer`;

const PIPELINE = `SELECT layer, mode FROM telemetry.dash_pipeline_layers ORDER BY position`;

const AUTH_REASONS = (): string => `
SELECT reason, count(*) AS count FROM telemetry.dash_auth_failures WHERE ${WINDOW()}
GROUP BY reason ORDER BY count(*) DESC, reason LIMIT 8`;

const AUTH_TREND = (): string => `
SELECT ${EPOCH(BIN)} AS bucket, count(*) AS count FROM telemetry.dash_auth_failures
WHERE ${WINDOW()} GROUP BY 1 ORDER BY 1`;

const CLOCK = `SELECT ${EPOCH("NOW_EXPRESSION")} AS now`;

type Row = Record<string, string | number | null>;
const num = (value: string | number | null | undefined): number => Number(value ?? 0);
const maybe = (value: string | number | null | undefined): number | null => (value === null || value === undefined ? null : Number(value));
const iso = (epoch: number): string => new Date(epoch * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");

/** Every bucket from the window's start to its end, with zeros where nothing happened, so a quiet
 * hour is a flat line and not a gap that a line chart would draw across. */
export function bucketEdges(nowEpoch: number, windowSeconds: number, bucketSeconds: number): number[] {
  const first = Math.floor((nowEpoch - windowSeconds) / bucketSeconds) * bucketSeconds;
  const last = Math.floor(nowEpoch / bucketSeconds) * bucketSeconds;
  const edges: number[] = [];
  for (let edge = first; edge <= last; edge += bucketSeconds) edges.push(edge);
  return edges;
}

export async function getCharts(session: AuthedSession, range: Range): Promise<Charts> {
  assertAuthed(session);
  const windowSeconds = RANGES[range];
  const bucketSeconds = BUCKET_SECONDS[range];
  const window = [windowSeconds, bucketSeconds];
  const result = await readOnly(session, async (client) => ({
    now: (await client.query(CLOCK.replace("NOW_EXPRESSION", nowSql()))).rows[0] as Row,
    series: (await client.query(TIME_SERIES(), window)).rows as Row[],
    tools: (await client.query(TOOLS(), [windowSeconds])).rows as Row[],
    blocked: (await client.query(BLOCKED_BY_LAYER(), [windowSeconds])).rows as Row[],
    wouldBlock: (await client.query(WOULD_BLOCK_BY_LAYER(), [windowSeconds])).rows as Row[],
    pipeline: (await client.query(PIPELINE)).rows as Row[],
    reasons: (await client.query(AUTH_REASONS(), [windowSeconds])).rows as Row[],
    trend: (await client.query(AUTH_TREND(), window)).rows as Row[],
  }));

  const nowEpoch = num(result.now.now);
  const edges = bucketEdges(nowEpoch, windowSeconds, bucketSeconds);
  const byBucket = new Map(result.series.map((row) => [num(row.bucket), row]));
  const trendByBucket = new Map(result.trend.map((row) => [num(row.bucket), num(row.count)]));

  const volume: VolumePoint[] = edges.map((edge) => ({
    ts: iso(edge),
    forwarded: num(byBucket.get(edge)?.forwarded),
    blocked: num(byBucket.get(edge)?.blocked),
  }));
  const latency: LatencyPoint[] = edges.map((edge) => {
    const row = byBucket.get(edge);
    return { ts: iso(edge), p50: maybe(row?.p50), p95: maybe(row?.p95), p99: maybe(row?.p99) };
  });
  const authTrend: TrendPoint[] = edges.map((edge) => ({ ts: iso(edge), count: trendByBucket.get(edge) ?? 0 }));
  const tools: ToolOutcome[] = result.tools.map((row) => ({ tool: String(row.tool), ok: num(row.ok), error: num(row.error) }));
  const authReasons: ReasonCount[] = result.reasons.map((row) => ({ reason: String(row.reason), count: num(row.count) }));

  // The layers of the configuration in use, in pipeline order (a layer that exists and blocked nothing
  // shows as zero), then any other layer that blocked something in this window (an older configuration).
  const blocked = new Map(result.blocked.map((row) => [String(row.layer), num(row.blocked)]));
  const wouldBlock = new Map(result.wouldBlock.map((row) => [String(row.layer), num(row.would_block)]));
  const configured = result.pipeline.map((row) => ({ layer: String(row.layer), mode: row.mode === null ? null : String(row.mode) }));
  const known = new Set(configured.map((row) => row.layer));
  const extra = [...new Set([...blocked.keys(), ...wouldBlock.keys()])].filter((layer) => !known.has(layer)).sort();
  const layers: LayerCount[] = [...configured, ...extra.map((layer) => ({ layer, mode: null }))].map(({ layer, mode }) => ({
    layer,
    mode,
    blocked: blocked.get(layer) ?? 0,
    wouldBlock: wouldBlock.get(layer) ?? 0,
  }));

  return { windowSeconds, bucketSeconds, start: iso(edges[0] ?? nowEpoch), end: iso(nowEpoch), volume, latency, tools, layers, authReasons, authTrend };
}
