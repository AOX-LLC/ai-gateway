import type { PoolClient } from "pg";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { UsagePanel } from "@/components/UsagePanel";
import { AuthError, mint } from "@/lib/auth/authed";
import { setPool } from "@/lib/db";
import { getUsage } from "@/lib/data";
import type { Usage, UsageMode } from "@/lib/data/types";

const session = mint({ sid: "s", iat: 0, seen: 0 });
const NOW = Date.UTC(2026, 9, 3, 12, 0, 0) / 1000;

beforeAll(() => {
  process.env.DASHBOARD_DATABASE_URL = "postgresql://x:y@db:5432/z";
  process.env.DASHBOARD_SESSION_SECRET = "u".repeat(40);
});
afterEach(() => setPool(undefined));

function fakePool(replies: Record<string, Record<string, unknown>[]>) {
  const sent: { text: string; values?: unknown[] }[] = [];
  const client = {
    query: async (text: string, values?: unknown[]) => {
      sent.push({ text, values });
      const hit = Object.entries(replies).find(([needle]) => text.includes(needle));
      return { rows: text.startsWith("BEGIN") || text.startsWith("SET") || text === "COMMIT" ? [] : (hit?.[1] ?? []) };
    },
    release: () => undefined,
  } as unknown as PoolClient;
  return { pool: { connect: async () => client }, sent };
}

describe("getUsage", () => {
  it("refuses a missing or forged session before the database is touched", async () => {
    let touched = false;
    setPool({ connect: async () => ((touched = true), {} as PoolClient) } as never);
    for (const bad of [undefined, null, {}, { sid: "forged" }, "session"]) {
      await expect(getUsage(bad as never, "24h"), JSON.stringify(bad)).rejects.toBeInstanceOf(AuthError);
    }
    expect(touched).toBe(false);
  });

  it("totals by mode, keeps replayed cost out of billed cost, counts replay misses and fills quiet buckets", async () => {
    const { pool, sent } = fakePool({
      "AS now": [{ now: String(NOW) }],
      "GROUP BY mode": [
        { mode: "replay", calls: "10", unrecorded: "2", input_tokens: "1000", output_tokens: "200", cache_read_tokens: "50", cache_write_tokens: "5", cost_usd: "0.01230000" },
        { mode: "live", calls: "3", unrecorded: "0", input_tokens: "300", output_tokens: "60", cache_read_tokens: "0", cache_write_tokens: "7", cost_usd: "0.00400000" },
      ],
      "GROUP BY 1": [{ bucket: String(NOW - 900), input_tokens: "1300", output_tokens: "260", cache_read_tokens: "50", cache_write_tokens: "12", billed_cost_usd: "0.004", replayed_cost_usd: "0.0123" }],
    });
    setPool(pool as never);

    const usage = await getUsage(session, "1h");

    expect(usage.totals).toEqual({ calls: 13, inputTokens: 1300, outputTokens: 260, cacheReadTokens: 50, cacheWriteTokens: 12, billedCostUsd: 0.004, replayedCostUsd: 0.0123 });
    expect(usage.byMode).toEqual({ replay: { calls: 10, costUsd: 0.0123 }, record: { calls: 0, costUsd: 0 }, live: { calls: 3, costUsd: 0.004 } });
    expect(usage.unrecorded).toBe(2);
    expect(usage.series).toHaveLength(61);
    expect(usage.series.filter((p) => p.inputTokens > 0)).toHaveLength(1);
    expect(usage.series.find((p) => p.inputTokens > 0)).toMatchObject({ billedCostUsd: 0.004, replayedCostUsd: 0.0123 });
    expect(sent.find((q) => q.text.includes("GROUP BY mode"))?.text).toContain("telemetry.dash_model_usage");
    expect(sent.find((q) => q.text.includes("GROUP BY 1"))?.values).toEqual([3600, 60]);
  });

  it("reads an empty range as zeros, and ignores a mode it does not know", async () => {
    setPool(fakePool({ "AS now": [{ now: String(NOW) }], "GROUP BY mode": [{ mode: "mystery", calls: "4", unrecorded: "0", input_tokens: "9", output_tokens: "9", cache_read_tokens: "0", cache_write_tokens: "0", cost_usd: "1" }] }).pool as never);

    const usage = await getUsage(session, "24h");

    expect(usage.totals.calls).toBe(0);
    expect(usage.totals.billedCostUsd).toBe(0);
  });
});

const point = (ts: string) => ({ ts, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, billedCostUsd: 0, replayedCostUsd: 0 });
const usageWith = (modes: Partial<Record<UsageMode, { calls: number; costUsd: number }>>, unrecorded = 0): Usage => {
  const byMode = { replay: { calls: 0, costUsd: 0 }, record: { calls: 0, costUsd: 0 }, live: { calls: 0, costUsd: 0 }, ...modes };
  return {
    windowSeconds: 86_400,
    bucketSeconds: 900,
    start: "2026-10-02T12:00:00Z",
    end: "2026-10-03T12:00:00Z",
    totals: {
      calls: byMode.replay.calls + byMode.record.calls + byMode.live.calls,
      inputTokens: 12_345,
      outputTokens: 678,
      cacheReadTokens: 90,
      cacheWriteTokens: 4,
      billedCostUsd: byMode.record.costUsd + byMode.live.costUsd,
      replayedCostUsd: byMode.replay.costUsd,
    },
    byMode,
    unrecorded,
    series: [point("2026-10-02T12:00:00Z"), point("2026-10-02T12:15:00Z")],
  };
};
const html = (element: React.ReactElement) => renderToStaticMarkup(element);
const panel = (usage: Usage | null, extra: { error?: string | null; stale?: boolean; since?: string | null } = {}) =>
  html(<UsagePanel usage={usage} error={extra.error ?? null} stale={extra.stale ?? false} since={extra.since ?? null} />);
/** The text with the one allowed use of the word removed: "not billed". */
const withoutNotBilled = (markup: string) => markup.replaceAll(/not billed/gi, "");

describe("the usage panel, replay only", () => {
  const markup = panel(usageWith({ replay: { calls: 10, costUsd: 0.0123 } }, 2));

  it("says the cost was replayed and not billed, and never calls it billed", () => {
    expect(markup).toContain("$0.0123");
    expect(markup).toContain("replayed, not billed");
    expect(markup).toContain("Replayed, not billed");
    expect(withoutNotBilled(markup)).not.toMatch(/billed/i);
  });

  it("gives mode as a word and shows the numbers with their units", () => {
    expect(markup).toContain("Replay");
    expect(markup).toContain("10 calls · $0.0123 replayed, not billed");
    expect(markup).toContain("12,345");
    expect(markup).toContain("Input tokens");
    expect(markup).toContain("<h2");
  });
});

describe("the usage panel, record and live", () => {
  it("labels the cost billed, as real spend", () => {
    const markup = panel(usageWith({ live: { calls: 3, costUsd: 0.5 }, record: { calls: 1, costUsd: 0.25 } }));

    expect(markup).toContain("$0.7500");
    expect(markup).toContain("billed");
    expect(markup).not.toContain("replayed");
    expect(markup).not.toContain("Replayed");
  });
});

describe("the usage panel, a mixed range", () => {
  const markup = panel(usageWith({ replay: { calls: 10, costUsd: 0.0123 }, live: { calls: 3, costUsd: 0.004 } }));

  it("shows billed and replayed cost as two figures and never adds them", () => {
    expect(markup).toContain("$0.0040");
    expect(markup).toContain("$0.0123");
    expect(markup).toContain("3 calls · $0.0040 billed");
    expect(markup).toContain("10 calls · $0.0123 replayed, not billed");
    expect(markup).not.toContain("$0.0163");
  });
});

describe("unclassified calls", () => {
  it("are counted on their own with a one-line reason", () => {
    const markup = panel(usageWith({ replay: { calls: 10, costUsd: 0.01 } }, 4));

    expect(markup).toContain("Unclassified");
    expect(markup).toMatch(/Unclassified<\/dt><dd class="usage-value">4/);
    expect(markup).toContain("not judged");
    expect(markup).toContain("replay had no recording for the call, so the classifier did not judge it.");
  });

  it("show zero without the warning when there are none, so the layout does not move", () => {
    const markup = panel(usageWith({ live: { calls: 1, costUsd: 0.1 } }, 0));

    expect(markup).toMatch(/Unclassified<\/dt><dd class="usage-value">0</);
    expect(markup).not.toContain("not judged");
  });
});

describe("the usage panel's other states", () => {
  it("is empty, said truthfully: the classifier exists and has not called a model in the range", () => {
    const markup = panel(usageWith({}));

    expect(markup).toContain("No model calls in this range");
    expect(markup).toContain("The injection classifier is active");
    expect(markup).toContain("last 24 hours");
    expect(markup).not.toMatch(/not active yet/i);
    expect(markup).toContain('role="status"');
    expect(markup).toContain("<h2");
  });

  it("is an error with no data", () => {
    const markup = panel(null, { error: "The database did not answer." });

    expect(markup).toContain("Could not read this panel");
    expect(markup).toContain("The database did not answer.");
    expect(markup).toContain('role="alert"');
  });

  it("keeps the last good data and says how old it is when a refresh fails", () => {
    const markup = panel(usageWith({ live: { calls: 3, costUsd: 0.5 } }), { error: "The database did not answer.", stale: true, since: "10:00:05" });

    expect(markup).toContain("Not updated since 10:00:05 UTC");
    expect(markup).toContain("$0.5000");
  });
});
