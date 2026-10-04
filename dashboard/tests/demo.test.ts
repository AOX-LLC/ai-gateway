import type { PoolClient } from "pg";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { mint } from "@/lib/auth/authed";
import { setPool } from "@/lib/db";

const session = mint({ sid: "s", iat: 0, seen: 0 });

beforeAll(() => {
  process.env.DASHBOARD_DATABASE_URL = "postgresql://x:y@db:5432/z";
  process.env.DASHBOARD_SESSION_SECRET = "m".repeat(40);
  process.env.DASHBOARD_DEMO = "1";
});
afterEach(() => setPool(undefined));

function sentSql() {
  const sent: string[] = [];
  const client = {
    query: async (text: string) => {
      sent.push(text);
      return { rows: [{ at: "2026-10-03T12:00:00.000000Z", requests: "0", forwarded: "0", succeeded: "0", blocked: "0", p95_ms: null, would_block: "0", auth_failures: "0", now: "1790000000" }] };
    },
    release: () => undefined,
  } as unknown as PoolClient;
  setPool({ connect: async () => client } as never);
  return sent;
}

describe("a demo stack", () => {
  it("measures every window and every expiry from the newest recorded request, not from the clock", async () => {
    const sent = sentSql();
    const data = await import("@/lib/data");

    await data.getKpis(session, "24h");
    await data.getDecisions(session, { range: "24h" });
    await data.getApprovals(session);
    await data.getCharts(session, "7d");
    const at = await data.getClock(session);

    const queries = sent.filter((q) => /FROM\s/i.test(q));
    expect(queries.length).toBeGreaterThanOrEqual(8);
    for (const query of queries) expect(query, query.slice(0, 80)).not.toMatch(/\bnow\(\)/i);
    // Every query that has a time window or an expiry takes it from the newest request.
    const windowed = queries.filter((q) => /make_interval|expires_at|AS now/.test(q));
    expect(windowed.length).toBeGreaterThanOrEqual(8);
    for (const query of windowed) expect(query, query.slice(0, 80)).toContain("max(ts) FROM telemetry.dash_requests");
    expect(at).toBe("2026-10-03T12:00:00.000000Z");
  });
});
