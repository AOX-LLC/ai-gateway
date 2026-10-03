import { type LegendItem } from "./Legend";
import { linePath, niceAxis, robustCeiling, timeTicks } from "./scale";

export type LineSeries = { id: string; label: string; cls: string; values: (number | null)[] };

type Props = {
  times: string[];
  series: LineSeries[];
  format: (value: number) => string;
  description: string;
  /** A wide chart is drawn at the width of the full-width panel and a half chart at the width of a
   * half panel, so text is the same size on screen in both. */
  wide?: boolean;
  height?: number;
  /** Counts of things: the axis labels are whole numbers. */
  whole?: boolean;
  /** Spikes that would flatten the rest are cut off at a ceiling and labelled, never silently. */
  clipSpikes?: { unit: string };
};

export const WIDE_WIDTH = 1120;
export const HALF_WIDTH = 540;
const M = { left: 48, right: 12, top: 10, bottom: 26 };

const epoch = (iso: string): number => Date.parse(iso) / 1000;

/** A line chart drawn as SVG on the server: horizontal gridlines, mono tick labels, one line per
 * series in the chart tokens. The y axis starts at zero. Gaps in a series are gaps in the line. */
export function TimeLineChart({ times, series, format, description, wide = false, height = 220, whole = false, clipSpikes }: Props) {
  const W = wide ? WIDE_WIDTH : HALF_WIDTH;
  const H = height;
  const first = epoch(times[0] ?? "");
  const last = epoch(times.at(-1) ?? "");
  const all = series.flatMap((s) => s.values.map((v) => v ?? 0));
  const highest = Math.max(0, ...all);
  const ceiling = clipSpikes ? robustCeiling(all) : null;
  const axis = niceAxis(ceiling ?? highest, 4, whole);
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;
  const toX = (t: number) => M.left + (last > first ? ((t - first) / (last - first)) * plotW : plotW / 2);
  const toY = (v: number) => M.top + plotH - (Math.min(v, axis.max) / axis.max) * plotH;
  const ticks = timeTicks(first, last, wide ? 6 : 5);
  const peak = ceiling === null ? null : peakOf(series, times);
  const peakX = peak ? toX(peak.at) : 0;
  const labelAtEnd = peakX > M.left + plotW / 2;
  return (
    <svg className="pui-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={description}>
      <title>{description}</title>
      {axis.ticks.map((tick) => (
        <g key={tick}>
          <line className="pui-chart-grid" x1={M.left} x2={W - M.right} y1={toY(tick)} y2={toY(tick)} />
          <text className="pui-anchor-end" x={M.left - 8} y={toY(tick) + 4}>
            {format(tick)}
          </text>
        </g>
      ))}
      <line className="pui-chart-axis" x1={M.left} x2={W - M.right} y1={H - M.bottom} y2={H - M.bottom} />
      {ticks.map((tick) => (
        <text key={tick.at} className="pui-anchor-mid" x={toX(tick.at)} y={H - 8}>
          {tick.label}
        </text>
      ))}
      {series.map((s) => (
        <path key={s.id} className={`pui-line chart-line ${s.cls}`} d={linePath(s.values.map((v, i) => [epoch(times[i] ?? ""), v] as const), toX, toY)} />
      ))}
      {peak && clipSpikes ? (
        <g>
          <path className="chart-clip-mark" d={`M${peakX - 4} ${M.top + 8}L${peakX + 4} ${M.top + 8}L${peakX} ${M.top}Z`} />
          <text className={`chart-clip-label ${labelAtEnd ? "pui-anchor-end" : ""}`} x={peakX + (labelAtEnd ? -8 : 8)} y={M.top + 9}>
            {`Peak ${format(peak.value)} ${clipSpikes.unit}, above this scale`}
          </text>
        </g>
      ) : null}
    </svg>
  );
}

/** The highest value of any series, and when it happened. */
function peakOf(series: LineSeries[], times: string[]): { value: number; at: number } | null {
  let best: { value: number; at: number } | null = null;
  for (const s of series) {
    s.values.forEach((v, i) => {
      if (v !== null && (best === null || v > best.value)) best = { value: v, at: epoch(times[i] ?? "") };
    });
  }
  return best;
}

export const lineLegend = (items: { label: string; series: number; icon: LegendItem["icon"] }[]): LegendItem[] => items;
