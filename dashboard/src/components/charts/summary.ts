import { formatInt, formatMs, windowLabel, words } from "@/lib/format";
import type { Charts } from "@/lib/data/types";

/** One sentence per chart saying what it shows, for the chart's accessible name and for anyone who
 * would rather read than look. Plain words, with units. */

export function volumeSummary(charts: Charts): string {
  const forwarded = charts.volume.reduce((sum, p) => sum + p.forwarded, 0);
  const blocked = charts.volume.reduce((sum, p) => sum + p.blocked, 0);
  if (forwarded + blocked === 0) return `No tool calls in the last ${windowLabel(charts.windowSeconds)}.`;
  const busiest = charts.volume.reduce((best, p) => (p.forwarded + p.blocked > best.forwarded + best.blocked ? p : best));
  return `${formatInt(forwarded)} forwarded and ${formatInt(blocked)} blocked over the last ${windowLabel(charts.windowSeconds)}; busiest at ${busiest.ts.slice(11, 16)} UTC with ${formatInt(busiest.forwarded + busiest.blocked)} calls.`;
}

export function latencySummary(charts: Charts): string {
  const seen = charts.latency.filter((p) => p.p95 !== null);
  if (seen.length === 0) return `No calls to measure in the last ${windowLabel(charts.windowSeconds)}.`;
  const worst = Math.max(...seen.map((p) => p.p99 ?? 0));
  const typical = [...seen].map((p) => p.p50 ?? 0).sort((a, b) => a - b)[Math.floor(seen.length / 2)] ?? 0;
  return `Typical call about ${formatMs(typical)} ms; the slowest 1% reached ${formatMs(worst)} ms.`;
}

export function toolsSummary(charts: Charts): string {
  if (charts.tools.length === 0) return "No forwarded calls to count.";
  const errors = charts.tools.reduce((sum, t) => sum + t.error, 0);
  const total = charts.tools.reduce((sum, t) => sum + t.ok + t.error, 0);
  return `${formatInt(total)} forwarded calls across ${charts.tools.length} tools; ${errors === 0 ? "none came back as an upstream error" : `${formatInt(errors)} came back as an upstream error`}.`;
}

export function layersSummary(charts: Charts): string {
  const blocked = charts.layers.reduce((sum, l) => sum + l.blocked, 0);
  const would = charts.layers.reduce((sum, l) => sum + l.wouldBlock, 0);
  if (blocked + would === 0) return "No layer blocked or would have blocked a call in this window.";
  const top = [...charts.layers].sort((a, b) => b.blocked + b.wouldBlock - (a.blocked + a.wouldBlock))[0];
  return `${formatInt(blocked)} calls blocked and ${formatInt(would)} that would have been blocked; most from ${top ? words(top.layer).toLowerCase() : "one layer"}.`;
}

export function authSummary(charts: Charts): string {
  const total = charts.authReasons.reduce((sum, r) => sum + r.count, 0);
  if (total === 0) return "No failed sign-ins in this window.";
  const top = charts.authReasons[0];
  return `${formatInt(total)} failed sign-ins; the most common reason is ${top ? words(top.reason).toLowerCase() : "unknown"}.`;
}
