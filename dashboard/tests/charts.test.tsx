import type { PoolClient } from "pg";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { mint } from "@/lib/auth/authed";
import { setPool } from "@/lib/db";
import { bucketEdges, getCharts } from "@/lib/data/charts";
import type { Charts } from "@/lib/data/types";
import { ChartsSection } from "@/components/charts/ChartsSection";
import { authSummary, latencySummary, layersSummary, toolsSummary, volumeSummary } from "@/components/charts/summary";

const session = mint({ sid: "s", iat: 0, seen: 0 });
beforeAll(() => {
  process.env.DASHBOARD_DATABASE_URL = "postgresql://x:y@db:5432/z";
  process.env.DASHBOARD_SESSION_SECRET = "c".repeat(40);
});
afterEach(() => setPool(undefined));

const NOW = Date.UTC(2026, 9, 3, 12, 0, 0) / 1000;

describe("the chart buckets", () => {
  it("run from the window's first edge to the one holding now, on whole-bucket edges", () => {
    const edges = bucketEdges(NOW + 400, 3600, 900);

    expect(edges[0]).toBe(NOW - 3600);
    expect(edges.at(-1)).toBe(NOW);
    expect(edges).toHaveLength(5);
    expect(edges.every((e) => e % 900 === 0)).toBe(true);
  });
});

function fakePool(replies: Record<string, Record<string, unknown>[]>) {
  const sent: string[] = [];
  const client = {
    query: async (text: string) => {
      sent.push(text);
      const hit = Object.entries(replies).find(([needle]) => text.includes(needle));
      return { rows: text.startsWith("BEGIN") || text.startsWith("SET") || text === "COMMIT" ? [] : (hit?.[1] ?? []) };
    },
    release: () => undefined,
  } as unknown as PoolClient;
  return { pool: { connect: async () => client }, sent };
}

describe("getCharts", () => {
  it("fills the quiet buckets with zeros, keeps percentiles empty where nothing was measured, and merges the layers", async () => {
    const { pool } = fakePool({
      "AS now": [{ now: String(NOW) }],
      "percentile_cont": [{ bucket: String(NOW - 900), forwarded: "5", blocked: "2", p50: "10", p95: "40", p99: "90" }],
      "GROUP BY tool": [{ tool: "tickets__assign", ok: "4", error: "1" }],
      "AS blocked\nFROM": [{ layer: "scope", blocked: "3" }, { layer: "legacy", blocked: "1" }],
      "AS would_block": [{ layer: "rate_limit", would_block: "2" }],
      "dash_pipeline_layers": [{ layer: "scope", mode: "enforce" }, { layer: "allowlist", mode: "enforce" }, { layer: "rate_limit", mode: "monitor" }],
      "GROUP BY reason": [{ reason: "invalid_token", count: "7" }],
    });
    setPool(pool as never);

    const charts = await getCharts(session, "1h");

    expect(charts.volume).toHaveLength(61);
    expect(charts.volume.filter((p) => p.forwarded + p.blocked > 0)).toEqual([{ ts: new Date((NOW - 900) * 1000).toISOString().replace(".000Z", "Z"), forwarded: 5, blocked: 2 }]);
    expect(charts.latency.filter((p) => p.p95 !== null)).toHaveLength(1);
    expect(charts.latency[0]).toMatchObject({ p50: null, p95: null, p99: null });
    expect(charts.tools).toEqual([{ tool: "tickets__assign", ok: 4, error: 1 }]);
    expect(charts.layers).toEqual([
      { layer: "scope", mode: "enforce", blocked: 3, wouldBlock: 0 },
      { layer: "allowlist", mode: "enforce", blocked: 0, wouldBlock: 0 },
      { layer: "rate_limit", mode: "monitor", blocked: 0, wouldBlock: 2 },
      { layer: "legacy", mode: null, blocked: 1, wouldBlock: 0 },
    ]);
    expect(charts.authReasons).toEqual([{ reason: "invalid_token", count: 7 }]);
    expect(charts.end).toBe("2026-10-03T12:00:00Z");
  });

  it("asks only for what it needs, in one read-only transaction, with the percentiles leaving out approval holds", async () => {
    const { pool, sent } = fakePool({ "AS now": [{ now: String(NOW) }] });
    setPool(pool as never);

    await getCharts(session, "24h");

    expect(sent[0]).toBe("BEGIN READ ONLY");
    expect(sent.at(-1)).toBe("COMMIT");
    expect(sent.join("\n")).toContain("deny_code IS DISTINCT FROM 'approval_pending'");
    for (const text of sent.filter((t) => /FROM\s/i.test(t))) expect(text).not.toMatch(/\b(FROM|JOIN)\s+dash_/i);
  });
});

const charts: Charts = {
  windowSeconds: 3600,
  bucketSeconds: 900,
  start: "2026-10-03T11:00:00Z",
  end: "2026-10-03T12:00:00Z",
  volume: [0, 1, 2, 3, 4].map((i) => ({ ts: new Date((NOW - 3600 + i * 900) * 1000).toISOString().replace(".000Z", "Z"), forwarded: i * 4, blocked: i })),
  latency: [0, 1, 2, 3, 4].map((i) => ({ ts: new Date((NOW - 3600 + i * 900) * 1000).toISOString().replace(".000Z", "Z"), p50: i === 2 ? null : 10 + i, p95: i === 2 ? null : 40 + i, p99: i === 2 ? null : 90 + i })),
  tools: [{ tool: "tickets__create_ticket", ok: 30, error: 2 }, { tool: "crm__get_account", ok: 12, error: 0 }],
  layers: [{ layer: "scope", mode: "enforce", blocked: 5, wouldBlock: 0 }, { layer: "rate_limit", mode: "monitor", blocked: 0, wouldBlock: 3 }, { layer: "allowlist", mode: "enforce", blocked: 0, wouldBlock: 0 }],
  authReasons: [{ reason: "invalid_token", count: 9 }, { reason: "expired", count: 2 }],
  authTrend: [0, 1, 2, 3, 4].map((i) => ({ ts: new Date((NOW - 3600 + i * 900) * 1000).toISOString().replace(".000Z", "Z"), count: i })),
};
const html = (value: Charts | null, props: { error?: string | null; stale?: boolean } = {}) => renderToStaticMarkup(<ChartsSection charts={value} error={props.error ?? null} stale={props.stale ?? false} since={null} />);

describe("the chart panels", () => {
  it("each draw an SVG with a plain-words description, and offer the same numbers as a table", () => {
    const markup = html(charts);

    expect((markup.match(/role="img"/g) ?? []).length).toBe(6); // five panels, the auth one has two drawings
    for (const title of ["Request volume", "Latency", "Success and errors by tool", "Blocked by layer", "Failed sign-ins"]) expect(markup).toContain(title);
    expect((markup.match(/<details/g) ?? []).length).toBe(6);
    expect(markup).toContain("Request volume as a table");
    expect(markup).toContain("Latency as a table");
    expect(markup).toContain("Blocked by layer as a table");
    expect(markup).toContain("<caption");
    expect(markup).toContain(volumeSummary(charts).replace("&", "&amp;"));
  });

  it("never rely on colour alone: every key item has an icon and a word, and bars end in words", () => {
    const markup = html(charts);

    for (const word of ["Forwarded", "Blocked", "Would block", "Answered", "Upstream error", "Median", "95th percentile", "99th percentile"]) expect(markup).toContain(word);
    expect(markup).toContain("30 answered · 2 errors");
    expect(markup).toContain("5 blocked");
    expect(markup).toContain("3 would block");
    expect(markup).toContain("Rate limit (monitor)");
    expect(markup).toContain("9 failed");
    expect(markup.match(/<svg class="pui-icon pui-icon--sm"/g)?.length).toBeGreaterThanOrEqual(10);
  });

  it("tells the lines of a chart apart by more than colour: each has its own dash pattern", () => {
    const markup = html(charts);
    const lineClasses = [...markup.matchAll(/<path class="pui-line chart-line (pui-s\d)"/g)].map((m) => m[1]);

    // volume: forwarded solid, blocked dashed; latency: median solid, p95 dashed, p99 dotted; failures: one line.
    expect(lineClasses.filter((c) => c === "pui-s1").length).toBeGreaterThanOrEqual(2);
    expect(lineClasses.filter((c) => c === "pui-s5").length).toBe(2);
    expect(lineClasses.filter((c) => c === "pui-s6").length).toBe(1);
    expect(markup).not.toMatch(/chart-line pui-s[234]"/);
    for (const key of [5, 6]) expect(markup).toContain(`data-series="${key}"`);
  });

  it("use only classes and attributes: no inline style, which the CSP forbids, and no colours of their own", () => {
    const markup = html(charts);

    expect(markup).not.toMatch(/\sstyle=/);
    expect(markup).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(markup).not.toMatch(/<linearGradient|<pattern|<filter/);
  });

  it("show an empty state per panel when there is nothing, and an error with no data", () => {
    const quiet: Charts = { ...charts, volume: charts.volume.map((p) => ({ ...p, forwarded: 0, blocked: 0 })), tools: [], authReasons: [], layers: [] };
    const markup = html(quiet);

    expect(markup).toContain("No tool calls in this window");
    expect(markup).toContain("No forwarded calls");
    expect(markup).toContain("No failed sign-ins");
    expect(markup).toContain("No layers in the configuration yet");
    expect(html(null, { error: "The database did not answer." })).toContain("Could not read this panel");
    expect(html(charts, { error: "x", stale: true })).toContain("Not updated");
  });

  it("keep a layer that blocked nothing in the chart as a zero row", () => {
    expect(html(charts)).toContain("0 blocked");
  });
});

describe("the chart descriptions", () => {
  it("say what the chart shows in a sentence with units", () => {
    expect(volumeSummary(charts)).toContain("forwarded and");
    expect(latencySummary(charts)).toMatch(/ms/);
    expect(toolsSummary(charts)).toContain("2 came back as an upstream error");
    expect(layersSummary(charts)).toContain("5 calls blocked and 3 that would have been blocked");
    expect(authSummary(charts)).toContain("11 failed sign-ins");
    expect(volumeSummary({ ...charts, volume: [] })).toContain("No tool calls");
    expect(authSummary({ ...charts, authReasons: [] })).toBe("No failed sign-ins in this window.");
  });
});
