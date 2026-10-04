/** What the panels get: explicit fields only, never a database row as it came. Counts and times
 * are numbers and strings, so they cross to the browser unchanged. */

export type Kpis = {
  windowSeconds: number;
  requests: number;
  perMinute: number;
  /** null: no call went through in the window, so there is nothing to take a percentile of. */
  p95Ms: number | null;
  forwarded: number;
  succeeded: number;
  /** null: nothing was forwarded. */
  successRate: number | null;
  blocked: number;
  wouldBlock: number;
  authFailures: number;
};

export type DecisionOutcome = "forwarded" | "blocked";

export type Decision = {
  requestId: string;
  /** UTC, ISO 8601 with microseconds. */
  ts: string;
  clientName: string;
  tool: string;
  namespace: string | null;
  effect: string | null;
  outcome: DecisionOutcome;
  /** Which layer stopped the call, when one did. */
  blockedBy: string | null;
  denyCode: string | null;
  upstreamStatus: string | null;
  durationMs: number | null;
  /** Layers in monitor mode that would have stopped it. */
  wouldBlock: string[];
};

export type DecisionsPage = {
  decisions: Decision[];
  /** Pass back to get the next, older page; null on the last page. */
  nextCursor: string | null;
};

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired" | "consumed" | "cancelled";

export type Approval = {
  id: string;
  tool: string;
  /** The upstream server the tool belongs to. */
  namespace: string;
  clientName: string | null;
  status: ApprovalStatus;
  createdAt: string;
  expiresAt: string;
  resolvedAt: string | null;
  /** Display name of the approver who decided it; null while pending. */
  decidedBy: string | null;
};

export type Approvals = {
  pending: Approval[];
  recent: Approval[];
};

/** One point of a time chart: the start of its bucket, UTC ISO 8601 (seconds). */
export type VolumePoint = { ts: string; forwarded: number; blocked: number };
/** null: nothing went through in that bucket, so there is nothing to take a percentile of. */
export type LatencyPoint = { ts: string; p50: number | null; p95: number | null; p99: number | null };
export type ToolOutcome = { tool: string; ok: number; error: number };
export type LayerCount = {
  layer: string;
  /** The layer's mode in the configuration in use; null if the layer is not in it (old records). */
  mode: string | null;
  blocked: number;
  wouldBlock: number;
};
export type ReasonCount = { reason: string; count: number };
export type TrendPoint = { ts: string; count: number };

export type Charts = {
  windowSeconds: number;
  bucketSeconds: number;
  /** The window, UTC ISO 8601: what the x axes span. */
  start: string;
  end: string;
  volume: VolumePoint[];
  latency: LatencyPoint[];
  tools: ToolOutcome[];
  layers: LayerCount[];
  authReasons: ReasonCount[];
  authTrend: TrendPoint[];
};

export type UsageMode = "replay" | "record" | "live";

/** Calls and cost of one mode. Cost is the recorded cost: only `record` and `live` was billed. */
export type ModeUsage = { calls: number; costUsd: number };

/** One bucket of the usage chart. Billed and replayed cost are kept apart: they are never added. */
export type UsagePoint = {
  ts: string;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  /** Cost of `record` and `live` calls: real spend. */
  billedCostUsd: number;
  /** Cost the recordings carry for `replay` calls: nothing was charged. */
  replayedCostUsd: number;
};

/** Model usage in the window: counts and cost only, never any text that was judged. */
export type Usage = {
  windowSeconds: number;
  bucketSeconds: number;
  start: string;
  end: string;
  totals: {
    calls: number;
    inputTokens: number;
    outputTokens: number;
    cacheReadTokens: number;
    cacheWriteTokens: number;
    /** Spend: `record` plus `live`. Replayed cost is not in it. */
    billedCostUsd: number;
    /** Cost carried by `replay` calls: not billed. */
    replayedCostUsd: number;
  };
  byMode: Record<UsageMode, ModeUsage>;
  /** Replay misses: the classifier had no recording, so the call was not judged. */
  unrecorded: number;
  series: UsagePoint[];
};

/** A panel's data, or the fact that it could not be read. The message is generic: what the database
 * said is logged on the server and never sent to the browser. */
export type Result<T> = { ok: true; data: T } | { ok: false; error: string };

export type Overview = {
  at: string;
  kpis: Result<Kpis>;
  charts: Result<Charts>;
  decisions: Result<DecisionsPage>;
  approvals: Result<Approvals>;
  usage: Result<Usage>;
};
