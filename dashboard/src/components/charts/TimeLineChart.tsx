import { type LegendItem } from "./Legend";
import { linePath, niceAxis, timeTicks } from "./scale";

export type LineSeries = { id: string; label: string; cls: string; values: (number | null)[] };

type Props = { times: string[]; series: LineSeries[]; format: (value: number) => string; description: string; height?: number };

const W = 640;
const M = { left: 48, right: 12, top: 10, bottom: 26 };

const epoch = (iso: string): number => Date.parse(iso) / 1000;

/** A line chart drawn as SVG on the server: horizontal gridlines, mono tick labels, one line per
 * series in the chart tokens. The y axis starts at zero. Gaps in a series are gaps in the line. */
export function TimeLineChart({ times, series, format, description, height = 220 }: Props) {
  const H = height;
  const first = epoch(times[0] ?? "");
  const last = epoch(times.at(-1) ?? "");
  const top = Math.max(0, ...series.flatMap((s) => s.values.map((v) => v ?? 0)));
  const axis = niceAxis(top);
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;
  const toX = (t: number) => M.left + (last > first ? ((t - first) / (last - first)) * plotW : plotW / 2);
  const toY = (v: number) => M.top + plotH - (v / axis.max) * plotH;
  const ticks = timeTicks(first, last);
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
        <path key={s.id} className={`pui-line ${s.cls}`} d={linePath(s.values.map((v, i) => [epoch(times[i] ?? ""), v] as const), toX, toY)} />
      ))}
    </svg>
  );
}

export const lineLegend = (items: { label: string; series: number; icon: LegendItem["icon"] }[]): LegendItem[] => items;
