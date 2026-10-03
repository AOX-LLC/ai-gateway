import { dayAndMinute, formatInt, formatMs, windowLabel, words } from "@/lib/format";
import type { Charts } from "@/lib/data/types";
import { ErrorState, StaleNote } from "../States";
import { BarRows } from "./BarRows";
import { ChartPanel } from "./ChartPanel";
import { Legend } from "./Legend";
import { TimeLineChart } from "./TimeLineChart";
import { authSummary, latencySummary, layersSummary, toolsSummary, volumeSummary } from "./summary";

type Props = { charts: Charts | null; error: string | null; stale: boolean; since: string | null };

const at = (iso: string) => dayAndMinute(iso);

export function ChartsSection({ charts, error, stale, since }: Props) {
  if (!charts) {
    return (
      <section aria-label="Charts">
        <ErrorState message={error ?? "No data."} />
      </section>
    );
  }
  const window = windowLabel(charts.windowSeconds);
  const times = charts.volume.map((p) => p.ts);
  const noCalls = charts.volume.every((p) => p.forwarded + p.blocked === 0);
  const emptyCalls = { icon: "activity" as const, title: "No tool calls in this window", text: "The chart fills as clients make calls." };
  const quietLayers = charts.layers.length === 0 ? { icon: "list" as const, title: "No layers in the configuration yet", text: "Layers appear here once the gateway has run with them." } : null;
  const noAuth = charts.authReasons.length === 0;

  return (
    <section aria-label="Charts" className="chart-section">
      {stale && error ? <StaleNote since={since} error={error} /> : null}
      <div className="chart-grid">
        <ChartPanel
          id="volume"
          wide
          title="Request volume"
          sub={`Forwarded and blocked tool calls · last ${window} · times in UTC`}
          empty={noCalls ? emptyCalls : null}
          legend={<Legend items={[{ label: "Forwarded", icon: "check", series: 1 }, { label: "Blocked", icon: "ban", series: 2 }]} />}
          tables={[{ caption: "Request volume as a table", head: ["Time (UTC)", "Forwarded", "Blocked"], rows: charts.volume.map((p) => [at(p.ts), p.forwarded, p.blocked]) }]}
        >
          <TimeLineChart
            wide
            height={240}
            whole
            times={times}
            format={(v) => formatInt(v)}
            description={volumeSummary(charts)}
            series={[
              { id: "forwarded", label: "Forwarded", cls: "pui-s1", values: charts.volume.map((p) => p.forwarded) },
              { id: "blocked", label: "Blocked", cls: "pui-s2", values: charts.volume.map((p) => p.blocked) },
            ]}
          />
        </ChartPanel>

        <ChartPanel
          id="latency"
          title="Latency"
          sub={`Gateway time per call: median, 95th and 99th percentile · last ${window}`}
          empty={noCalls ? emptyCalls : null}
          legend={<Legend items={[{ label: "Median", icon: "clock", series: 1 }, { label: "95th percentile", icon: "clock", series: 2 }, { label: "99th percentile", icon: "clock", series: 3 }]} />}
          note="Calls held for a person's approval are left out: that time is waiting, not slowness. Milliseconds."
          tables={[{ caption: "Latency as a table", head: ["Time (UTC)", "Median ms", "95th ms", "99th ms"], rows: charts.latency.map((p) => [at(p.ts), p.p50 === null ? "—" : formatMs(p.p50), p.p95 === null ? "—" : formatMs(p.p95), p.p99 === null ? "—" : formatMs(p.p99)]) }]}
        >
          <TimeLineChart
            clipSpikes={{ unit: "ms" }}
            height={200}
            times={times}
            format={(v) => formatInt(v)}
            description={latencySummary(charts)}
            series={[
              { id: "p50", label: "Median", cls: "pui-s1", values: charts.latency.map((p) => p.p50) },
              { id: "p95", label: "95th percentile", cls: "pui-s2", values: charts.latency.map((p) => p.p95) },
              { id: "p99", label: "99th percentile", cls: "pui-s3", values: charts.latency.map((p) => p.p99) },
            ]}
          />
        </ChartPanel>

        <ChartPanel
          id="tools"
          title="Success and errors by tool"
          sub={`Forwarded calls, by what the upstream answered · last ${window}`}
          empty={charts.tools.length === 0 ? { icon: "list", title: "No forwarded calls", text: "Tools appear here once the gateway has forwarded a call." } : null}
          legend={<Legend items={[{ label: "Answered", icon: "check", swatch: "swatch-ok" }, { label: "Upstream error", icon: "alert", swatch: "swatch-error" }]} />}
          tables={[{ caption: "Success and errors by tool as a table", head: ["Tool", "Answered", "Upstream errors"], rows: charts.tools.map((t) => [t.tool, t.ok, t.error]) }]}
        >
          <BarRows
            description={toolsSummary(charts)}
            rows={charts.tools.map((t) => ({
              label: t.tool,
              segments: [{ value: t.ok, cls: "pui-s3" }, { value: t.error, cls: "pui-s6" }],
              text: `${formatInt(t.ok)} answered${t.error > 0 ? ` · ${formatInt(t.error)} ${t.error === 1 ? "error" : "errors"}` : ""}`,
            }))}
          />
        </ChartPanel>

        <ChartPanel
          id="layers"
          title="Blocked by layer"
          sub={`Calls each layer stopped, and calls it would have stopped in monitor mode · last ${window}`}
          empty={quietLayers}
          legend={<Legend items={[{ label: "Blocked", icon: "ban", swatch: "swatch-blocked" }, { label: "Would block", icon: "alert", swatch: "swatch-would" }]} />}
          tables={[{ caption: "Blocked by layer as a table", head: ["Layer", "Mode", "Blocked", "Would block"], rows: charts.layers.map((l) => [words(l.layer), l.mode ?? "—", l.blocked, l.wouldBlock]) }]}
        >
          <BarRows
            description={layersSummary(charts)}
            rows={charts.layers.map((l) => ({
              label: `${words(l.layer)}${l.mode && l.mode !== "enforce" ? ` (${l.mode})` : ""}`,
              segments: [{ value: l.blocked, cls: "bar-blocked" }, { value: l.wouldBlock, cls: "bar-would" }],
              text: `${formatInt(l.blocked)} blocked${l.wouldBlock > 0 ? ` · ${formatInt(l.wouldBlock)} would block` : ""}`,
            }))}
          />
        </ChartPanel>

        <ChartPanel
          id="auth"
          title="Failed sign-ins"
          sub={`Failed authentications by reason, and when they happened · last ${window}`}
          empty={noAuth ? { icon: "check", title: "No failed sign-ins", text: "Every client that tried to connect had a valid token." } : null}
          legend={<Legend items={[{ label: "Failed sign-ins", icon: "ban", series: 1 }]} />}
          tables={[
            { caption: "Failed sign-ins by reason as a table", head: ["Reason", "Failed"], rows: charts.authReasons.map((r) => [words(r.reason), r.count]) },
            { caption: "Failed sign-ins over time as a table", head: ["Time (UTC)", "Failed"], rows: charts.authTrend.map((p) => [at(p.ts), p.count]) },
          ]}
        >
          <BarRows description={authSummary(charts)} rows={charts.authReasons.map((r) => ({ label: words(r.reason), segments: [{ value: r.count, cls: "pui-s1" }], text: `${formatInt(r.count)} failed` }))} />
          <TimeLineChart whole height={140} times={charts.authTrend.map((p) => p.ts)} format={(v) => formatInt(v)} description={`Failed sign-ins over the last ${window}: ${authSummary(charts)}`} series={[{ id: "auth", label: "Failed sign-ins", cls: "pui-s1", values: charts.authTrend.map((p) => p.count) }]} />
        </ChartPanel>
      </div>
    </section>
  );
}
