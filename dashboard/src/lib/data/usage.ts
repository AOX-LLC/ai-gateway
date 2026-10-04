import { type AuthedSession, assertAuthed } from "../auth/authed";
import { readOnly } from "../db";
import { bucketEdges } from "./charts";
import { nowSql } from "./clock";
import { BUCKET_SECONDS, RANGES, type Range } from "./ranges";
import type { ModeUsage, Usage, UsageMode, UsagePoint } from "./types";

const ORIGIN = "TIMESTAMPTZ '2000-01-01 00:00:00+00'";
const WINDOW = (): string => `ts >= ${nowSql()} - make_interval(secs => $1::int)`;

// Counts only: the view carries no text. A replayed call's cost is the recording's, so `replay` is
// kept out of the billed sums here, not left for the screen to remember.
const BY_MODE = (): string => `
SELECT mode,
       count(*) AS calls,
       count(*) FILTER (WHERE status = 'unrecorded') AS unrecorded,
       coalesce(sum(input_tokens), 0) AS input_tokens,
       coalesce(sum(output_tokens), 0) AS output_tokens,
       coalesce(sum(cache_read_tokens), 0) AS cache_read_tokens,
       coalesce(sum(cache_write_tokens), 0) AS cache_write_tokens,
       coalesce(sum(cost_usd), 0) AS cost_usd
FROM telemetry.dash_model_usage
WHERE ${WINDOW()}
GROUP BY mode`;

const SERIES = (): string => `
SELECT extract(epoch FROM date_bin(make_interval(secs => $2::int), ts, ${ORIGIN}))::bigint AS bucket,
       coalesce(sum(input_tokens), 0) AS input_tokens,
       coalesce(sum(output_tokens), 0) AS output_tokens,
       coalesce(sum(cache_read_tokens), 0) AS cache_read_tokens,
       coalesce(sum(cache_write_tokens), 0) AS cache_write_tokens,
       coalesce(sum(cost_usd) FILTER (WHERE mode <> 'replay'), 0) AS billed_cost_usd,
       coalesce(sum(cost_usd) FILTER (WHERE mode = 'replay'), 0) AS replayed_cost_usd
FROM telemetry.dash_model_usage
WHERE ${WINDOW()}
GROUP BY 1 ORDER BY 1`;

const CLOCK = (): string => `SELECT extract(epoch FROM ${nowSql()})::bigint AS now`;

type Row = Record<string, string | number | null>;
const num = (value: string | number | null | undefined): number => Number(value ?? 0);
const iso = (epoch: number): string => new Date(epoch * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
const MODES: UsageMode[] = ["replay", "record", "live"];

export async function getUsage(session: AuthedSession, range: Range): Promise<Usage> {
  assertAuthed(session);
  const windowSeconds = RANGES[range];
  const bucketSeconds = BUCKET_SECONDS[range];
  const result = await readOnly(session, async (client) => ({
    now: (await client.query(CLOCK())).rows[0] as Row,
    modes: (await client.query(BY_MODE(), [windowSeconds])).rows as Row[],
    series: (await client.query(SERIES(), [windowSeconds, bucketSeconds])).rows as Row[],
  }));

  const nowEpoch = num(result.now.now);
  const edges = bucketEdges(nowEpoch, windowSeconds, bucketSeconds);
  const byBucket = new Map(result.series.map((row) => [num(row.bucket), row]));
  const series: UsagePoint[] = edges.map((edge) => {
    const row = byBucket.get(edge);
    return {
      ts: iso(edge),
      inputTokens: num(row?.input_tokens),
      outputTokens: num(row?.output_tokens),
      cacheReadTokens: num(row?.cache_read_tokens),
      cacheWriteTokens: num(row?.cache_write_tokens),
      billedCostUsd: num(row?.billed_cost_usd),
      replayedCostUsd: num(row?.replayed_cost_usd),
    };
  });

  const byMode = { replay: { calls: 0, costUsd: 0 }, record: { calls: 0, costUsd: 0 }, live: { calls: 0, costUsd: 0 } } satisfies Record<UsageMode, ModeUsage>;
  const totals = { calls: 0, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, billedCostUsd: 0, replayedCostUsd: 0 };
  let unrecorded = 0;
  for (const row of result.modes) {
    const mode = String(row.mode) as UsageMode;
    if (!MODES.includes(mode)) continue;
    const costUsd = num(row.cost_usd);
    byMode[mode] = { calls: num(row.calls), costUsd };
    totals.calls += num(row.calls);
    totals.inputTokens += num(row.input_tokens);
    totals.outputTokens += num(row.output_tokens);
    totals.cacheReadTokens += num(row.cache_read_tokens);
    totals.cacheWriteTokens += num(row.cache_write_tokens);
    if (mode === "replay") totals.replayedCostUsd += costUsd;
    else totals.billedCostUsd += costUsd;
    unrecorded += num(row.unrecorded);
  }

  return { windowSeconds, bucketSeconds, start: iso(edges[0] ?? nowEpoch), end: iso(nowEpoch), totals, byMode, unrecorded, series };
}
