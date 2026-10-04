import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { PoolClient } from "pg";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { AuthError, mint } from "@/lib/auth/authed";
import { setPool } from "@/lib/db";
import * as data from "@/lib/data";
import { encodeCursor } from "@/lib/data/cursor";

const session = mint({ sid: "s", iat: 0, seen: 0 });

beforeAll(() => {
  process.env.DASHBOARD_DATABASE_URL = "postgresql://x:y@db:5432/z";
  process.env.DASHBOARD_SESSION_SECRET = "d".repeat(40);
});

type Reply = Record<string, unknown>[];

function poolReplying(replies: (text: string) => Reply) {
  const sent: { text: string; values?: unknown[] }[] = [];
  const client = {
    query: async (text: string, values?: unknown[]) => {
      sent.push({ text, values });
      return { rows: text.startsWith("BEGIN") || text.startsWith("SET") || text === "COMMIT" ? [] : replies(text) };
    },
    release: () => undefined,
  } as unknown as PoolClient;
  return { pool: { connect: async () => client }, sent };
}

afterEach(() => setPool(undefined));

describe("every data function", () => {
  it("is exported with a session first, and refuses a missing or forged one before the database is touched", async () => {
    let touched = false;
    setPool({ connect: async () => ((touched = true), {} as PoolClient) } as never);
    const functions = Object.entries(data);

    expect(functions.map(([name]) => name).sort()).toEqual(["getApprovals", "getCharts", "getClock", "getDecisions", "getKpis", "getUsage"]);
    for (const [name, call] of functions) {
      const fn = call as (...args: unknown[]) => Promise<unknown>;
      for (const bad of [undefined, null, {}, { sid: "forged" }, "session"]) {
        await expect(fn(bad, "24h"), `${name}(${JSON.stringify(bad)})`).rejects.toBeInstanceOf(AuthError);
      }
    }
    expect(touched).toBe(false);
  });
});

describe("the views", () => {
  it("are named with their schema, because the reader's search path does not include it", async () => {
    const { pool, sent } = poolReplying(() => [{ requests: "0", forwarded: "0", succeeded: "0", blocked: "0", p95_ms: null, would_block: "0", auth_failures: "0" }]);
    setPool(pool as never);

    await data.getKpis(session, "1h");
    await data.getDecisions(session, { range: "1h" });
    await data.getApprovals(session);

    const queries = sent.map((q) => q.text).filter((text) => /FROM\s/i.test(text)).join("\n");
    const mentioned = [...queries.matchAll(/\b(?:FROM|JOIN)\s+([a-z_.]+)/gi)].map((m) => m[1]!).filter((name) => name.startsWith("dash_") || name.includes("."));
    expect(mentioned.length).toBeGreaterThan(4);
    for (const name of mentioned) expect(name, `${name} must be schema-qualified`).toMatch(/^(telemetry|policy)\.dash_[a-z_]+$/);
    expect(queries).not.toMatch(/\b(FROM|JOIN)\s+dash_/i);
  });
});

describe("the data module", () => {
  it("exports every function that reads the database, so the session test above covers it", () => {
    const directory = fileURLToPath(new URL("../src/lib/data/", import.meta.url));
    const index = readFileSync(`${directory}index.ts`, "utf8");
    const readers = readdirSync(directory)
      .filter((file) => file.endsWith(".ts") && !["index.ts", "types.ts", "cursor.ts", "ranges.ts", "overview.ts"].includes(file))
      .flatMap((file) => [...readFileSync(`${directory}${file}`, "utf8").matchAll(/export async function (\w+)\(\s*(\w*)/g)].map((m) => [file, m[1]!, m[2]!]));

    expect(readers.length).toBeGreaterThanOrEqual(6);
    for (const [file, name, firstParameter] of readers) {
      expect(firstParameter, `${name} (${file}) is async, so it reads: it must take the session first`).toBe("session");
      expect(index, `${name} (${file}) must be exported from lib/data/index.ts`).toContain(name);
    }
  });
});

describe("the KPIs", () => {
  it("turn the counts into rates and leave a percentile empty when nothing went through", async () => {
    const { pool, sent } = poolReplying(() => [
      { requests: "120", forwarded: "100", succeeded: "90", blocked: "20", p95_ms: "41.5", would_block: "3", auth_failures: "7" },
    ]);
    setPool(pool as never);

    const kpis = await data.getKpis(session, "1h");

    expect(kpis).toMatchObject({ requests: 120, perMinute: 2, p95Ms: 41.5, successRate: 0.9, blocked: 20, wouldBlock: 3, authFailures: 7 });
    expect(sent.find((q) => q.text.includes("dash_requests"))?.values).toEqual([3600]);

    setPool(poolReplying(() => [{ requests: "0", forwarded: "0", succeeded: "0", blocked: "0", p95_ms: null, would_block: "0", auth_failures: "0" }]).pool as never);
    expect(await data.getKpis(session, "24h")).toMatchObject({ requests: 0, p95Ms: null, successRate: null });
  });
});

describe("the recent decisions", () => {
  const row = (n: number): Reply[number] => ({
    request_id: `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    ts: `2026-10-03T10:00:${String(60 - n).padStart(2, "0")}.000123Z`,
    client_name: "harborline-ops-bot",
    tool: "tickets__assign",
    namespace: "tickets",
    effect: "write",
    outcome: n === 1 ? "blocked" : "forwarded",
    blocked_by: n === 1 ? "approval" : "",
    deny_code: n === 1 ? "approval_pending" : "",
    upstream_status: "ok",
    duration_ms: "12.5",
    would_block: n === 2 ? ["rate_limit"] : [],
  });

  it("asks for one more than a page to know whether there is another, and cursors on the last row it shows", async () => {
    const { pool, sent } = poolReplying(() => [row(1), row(2), row(3)]);
    setPool(pool as never);

    const page = await data.getDecisions(session, { range: "24h", limit: 2 });

    expect(page.decisions).toHaveLength(2);
    expect(page.decisions[0]).toMatchObject({ outcome: "blocked", blockedBy: "approval", denyCode: "approval_pending" });
    expect(page.decisions[1]?.wouldBlock).toEqual(["rate_limit"]);
    expect(page.nextCursor).toBeTypeOf("string");
    expect(sent.find((q) => q.text.includes("LIMIT $4"))?.values).toEqual([86400, null, null, 3]);
  });

  it("has no next cursor on the last page, and pages from the cursor it is given", async () => {
    const cursor = encodeCursor({ ts: "2026-10-03T10:00:59.000123Z", id: "00000000-0000-4000-8000-000000000001" });
    const { pool, sent } = poolReplying(() => [row(2)]);
    setPool(pool as never);

    const page = await data.getDecisions(session, { range: "7d", cursor, limit: 25 });

    expect(page.nextCursor).toBeNull();
    expect(sent.find((q) => q.text.includes("LIMIT $4"))?.values).toEqual([
      604800,
      "2026-10-03T10:00:59.000123Z",
      "00000000-0000-4000-8000-000000000001",
      26,
    ]);
  });

  it("clamps the page size, and drops a cursor it did not make", async () => {
    const { pool, sent } = poolReplying(() => []);
    setPool(pool as never);

    await data.getDecisions(session, { range: "1h", limit: 9999, cursor: "'; DROP TABLE x; --" });
    await data.getDecisions(session, { range: "1h", limit: -5 });

    const limits = sent.filter((q) => q.text.includes("LIMIT $4")).map((q) => q.values);
    expect(limits).toEqual([[3600, null, null, 51], [3600, null, null, 2]]);
  });
});

describe("the approval queue", () => {
  it("separates what waits for a person from what was decided, with names and no ids of people", async () => {
    const base = { namespace: "tickets", client_name: "harborline-support-bot", created_at: "2026-10-03T10:00:00.000000Z", expires_at: "2026-10-03T10:30:00.000000Z", resolved_at: null, resolved_by_name: null };
    const { pool } = poolReplying((text) =>
      text.includes("status = 'pending' AND expires_at > now()") && !text.includes("NOT")
        ? [{ ...base, id: "a", action: "tickets__assign", status: "pending" }]
        : [{ ...base, id: "b", action: "tickets__assign", status: "approved", resolved_at: "2026-10-03T10:01:00.000000Z", resolved_by_name: "Dana Kerr" }],
    );
    setPool(pool as never);

    const approvals = await data.getApprovals(session);

    expect(approvals.pending.map((a) => [a.id, a.status, a.decidedBy])).toEqual([["a", "pending", null]]);
    expect(approvals.recent.map((a) => [a.id, a.status, a.decidedBy])).toEqual([["b", "approved", "Dana Kerr"]]);
  });

  it("reads a status it does not know as cancelled rather than trusting it", async () => {
    const { pool } = poolReplying(() => [{ id: "c", action: "x__y", namespace: "x", status: "weird", created_at: "t", expires_at: "t" }]);
    setPool(pool as never);

    expect((await data.getApprovals(session)).recent[0]?.status).toBe("cancelled");
  });
});
