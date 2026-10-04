import { dayAndMinute, formatCompact, formatInt, formatUsd, windowLabel } from "@/lib/format";
import type { Usage, UsageMode } from "@/lib/data/types";
import { ChartPanel } from "./charts/ChartPanel";
import { Legend, type LegendItem } from "./charts/Legend";
import { TimeLineChart } from "./charts/TimeLineChart";
import { Icon } from "./Icon";
import { ErrorState, NotActive, StaleNote } from "./States";

/** Tokens and cost of the model the classifier calls. Counts and money only: the data behind it holds
 * no text. The one rule this panel exists to keep: a replayed call's cost is the recording's, and
 * nothing was charged for it, so it is labelled "Replayed, not billed" and is never added to spend. */

type Props = { usage: Usage | null; error: string | null; stale: boolean; since: string | null };

export const REPLAYED = "replayed, not billed";
const MODE_LABEL: Record<UsageMode, string> = { live: "Live", record: "Record", replay: "Replay" };
const MODE_ORDER: UsageMode[] = ["live", "record", "replay"];

const plural = (count: number, one: string, many = `${one}s`) => `${formatInt(count)} ${count === 1 ? one : many}`;
const at = (iso: string) => dayAndMinute(iso);

/** Dollars on a chart axis, without the sign (the panel says "US dollars"): "0.0025", "12.5", "1.2K". */
function axisUsd(value: number): string {
  if (value >= 1000) return formatCompact(value);
  return value.toFixed(value >= 1 ? 2 : 4).replace(/0+$/, "").replace(/\.$/, "");
}

export function hasBilled(usage: Usage): boolean {
  return usage.byMode.record.calls + usage.byMode.live.calls > 0;
}

export function hasReplayed(usage: Usage): boolean {
  return usage.byMode.replay.calls > 0;
}

/** What one mode's row says: its calls, and its cost with the word that tells whether it was charged. */
export function modeText(usage: Usage, mode: UsageMode): string {
  const { calls, costUsd } = usage.byMode[mode];
  if (calls === 0) return "no calls";
  return `${plural(calls, "call")} · ${formatUsd(costUsd)} ${mode === "replay" ? REPLAYED : "billed"}`;
}

export function tokensSummary(usage: Usage): string {
  const { inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens } = usage.totals;
  return `${formatInt(inputTokens)} input and ${formatInt(outputTokens)} output tokens over the last ${windowLabel(usage.windowSeconds)}; ${formatInt(cacheReadTokens)} read from and ${formatInt(cacheWriteTokens)} written to the cache.`;
}

export function costSummary(usage: Usage): string {
  const parts: string[] = [];
  if (hasBilled(usage)) parts.push(`${formatUsd(usage.totals.billedCostUsd)} billed`);
  if (hasReplayed(usage)) parts.push(`${formatUsd(usage.totals.replayedCostUsd)} ${REPLAYED}`);
  return `${parts.join(" and ")} over the last ${windowLabel(usage.windowSeconds)}.`;
}

function Tile({ label, value, unit, foot }: { label: string; value: string; unit?: string; foot?: string }) {
  return (
    <div className="usage-tile">
      <dt className="pui-eyebrow">{label}</dt>
      <dd className="usage-value">
        {value}
        {unit ? <span className="pui-kpi-unit">{unit}</span> : null}
      </dd>
      {foot ? <dd className="pui-kpi-foot">{foot}</dd> : null}
    </div>
  );
}

function TokensPanel({ usage }: { usage: Usage }) {
  const { totals, series } = usage;
  const window = windowLabel(usage.windowSeconds);
  return (
    <ChartPanel
      id="tokens"
      title="Tokens"
      sub={`Input and output tokens of the classifier's model · last ${window} · times in UTC`}
      legend={<Legend items={[{ label: "Input", icon: "inbox", series: 1 }, { label: "Output", icon: "activity", series: 5 }]} />}
      note="Cache reads and writes are counted in the tiles and the table, not drawn."
      tables={[
        {
          caption: "Tokens as a table",
          head: ["Time (UTC)", "Input", "Output", "Cache read", "Cache write"],
          rows: series.map((p) => [at(p.ts), p.inputTokens, p.outputTokens, p.cacheReadTokens, p.cacheWriteTokens]),
        },
      ]}
    >
      <dl className="usage-tiles">
        <Tile label="Input tokens" value={formatInt(totals.inputTokens)} />
        <Tile label="Output tokens" value={formatInt(totals.outputTokens)} />
        <Tile label="Cache read tokens" value={formatInt(totals.cacheReadTokens)} />
        <Tile label="Cache write tokens" value={formatInt(totals.cacheWriteTokens)} />
      </dl>
      <TimeLineChart
        whole
        height={200}
        times={series.map((p) => p.ts)}
        format={formatCompact}
        description={tokensSummary(usage)}
        series={[
          { id: "input", label: "Input", cls: "pui-s1", values: series.map((p) => p.inputTokens) },
          { id: "output", label: "Output", cls: "pui-s5", values: series.map((p) => p.outputTokens) },
        ]}
      />
    </ChartPanel>
  );
}

function CostPanel({ usage }: { usage: Usage }) {
  const { totals, series } = usage;
  const window = windowLabel(usage.windowSeconds);
  const billed = hasBilled(usage);
  const replayed = hasReplayed(usage);
  const legend: LegendItem[] = [
    ...(billed ? [{ label: "Billed", icon: "coin" as const, series: 1 }] : []),
    ...(replayed ? [{ label: "Replayed, not billed", icon: "clock" as const, series: 5 }] : []),
  ];
  const lines = [
    ...(billed ? [{ id: "billed", label: "Billed", cls: "pui-s1", values: series.map((p) => p.billedCostUsd) }] : []),
    ...(replayed ? [{ id: "replayed", label: "Replayed, not billed", cls: "pui-s5", values: series.map((p) => p.replayedCostUsd) }] : []),
  ];
  return (
    <ChartPanel
      id="cost"
      title="Cost"
      sub={`What the model calls cost, by mode · last ${window} · US dollars`}
      legend={<Legend items={legend} />}
      note={replayed ? "Replay serves a committed recording: its cost is what the recorded call cost, and nothing was charged now." : "Record and live calls are billed by the model provider."}
      tables={[
        {
          caption: "Cost as a table",
          head: ["Time (UTC)", ...(billed ? ["Billed $"] : []), ...(replayed ? ["Replayed, not billed $"] : [])],
          rows: series.map((p) => [at(p.ts), ...(billed ? [p.billedCostUsd.toFixed(4)] : []), ...(replayed ? [p.replayedCostUsd.toFixed(4)] : [])]),
        },
      ]}
    >
      <dl className="usage-tiles">
        {billed ? <Tile label="Record and live" value={formatUsd(totals.billedCostUsd)} unit="billed" foot="Real spend with the model provider" /> : null}
        {replayed ? <Tile label="Replay" value={formatUsd(totals.replayedCostUsd)} unit={REPLAYED} foot="The recording's cost; nothing was charged" /> : null}
      </dl>
      <ul className="usage-modes" aria-label="Calls and cost by mode">
        {MODE_ORDER.map((mode) => (
          <li key={mode} className="usage-mode">
            <span className="usage-mode-name">{MODE_LABEL[mode]}</span>
            <span className="usage-mode-text">{modeText(usage, mode)}</span>
          </li>
        ))}
      </ul>
      <TimeLineChart height={160} times={series.map((p) => p.ts)} format={axisUsd} description={costSummary(usage)} series={lines} />
    </ChartPanel>
  );
}

function CallsPanel({ usage }: { usage: Usage }) {
  const window = windowLabel(usage.windowSeconds);
  return (
    <section className="pui-panel" aria-labelledby="calls-title" id="calls">
      <div className="pui-panel-header">
        <div className="pui-panel-heading">
          <h2 className="pui-panel-title" id="calls-title">
            Model calls
          </h2>
          <span className="pui-panel-sub">Calls to the classifier&apos;s model · last {window} · counts only, never the text judged</span>
        </div>
      </div>
      <div className="chart-body">
        <dl className="usage-tiles">
          <Tile label="Calls" value={formatInt(usage.totals.calls)} />
          <div className="usage-tile">
            <dt className="pui-eyebrow">Unclassified</dt>
            <dd className="usage-value">
              {formatInt(usage.unrecorded)}
              {usage.unrecorded > 0 ? (
                <span className="pui-badge pui-badge--warning usage-badge">
                  <Icon name="alert" size="sm" />
                  not judged
                </span>
              ) : null}
            </dd>
          </div>
        </dl>
        <p className="chart-note">Unclassified: replay had no recording for the call, so the classifier did not judge it.</p>
      </div>
    </section>
  );
}

export function UsagePanel({ usage, error, stale, since }: Props) {
  if (!usage) {
    return (
      <section aria-labelledby="usage-title">
        <h2 className="pui-sr-only" id="usage-title">
          Tokens and cost
        </h2>
        <ErrorState message={error ?? "No data."} />
      </section>
    );
  }
  if (usage.totals.calls === 0) {
    return (
      <section aria-labelledby="usage-title">
        <h2 className="pui-sr-only" id="usage-title">
          Tokens and cost
        </h2>
        {stale && error ? <StaleNote since={since} error={error} /> : null}
        <NotActive title="No model calls in this range">
          The injection classifier is active. It has not called a model in the last {windowLabel(usage.windowSeconds)}, so there are no tokens to count and no cost to show.
        </NotActive>
      </section>
    );
  }
  return (
    <section aria-label="Tokens and cost" className="chart-section">
      {stale && error ? <StaleNote since={since} error={error} /> : null}
      <div className="chart-grid">
        <TokensPanel usage={usage} />
        <CostPanel usage={usage} />
      </div>
      <CallsPanel usage={usage} />
    </section>
  );
}
