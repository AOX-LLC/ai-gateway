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

/** A panel's data, or the fact that it could not be read. The message is generic: what the database
 * said is logged on the server and never sent to the browser. */
export type Result<T> = { ok: true; data: T } | { ok: false; error: string };

export type Overview = {
  at: string;
  kpis: Result<Kpis>;
  decisions: Result<DecisionsPage>;
  approvals: Result<Approvals>;
};
