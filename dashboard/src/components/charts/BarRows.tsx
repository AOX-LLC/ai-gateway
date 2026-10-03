import { barLength } from "./scale";

export type BarSegment = { value: number; cls: string };
export type BarRow = { label: string; segments: BarSegment[]; text: string };

type Props = { rows: BarRow[]; description: string };

const W = 640;
const ROW = 30;
const LABEL = 150;
const TEXT = 170;

const shorten = (label: string, max = 22): string => (label.length > max ? `${label.slice(0, max - 1)}…` : label);

/** Horizontal bars, one row per category, drawn as SVG on the server. Every row ends in its numbers
 * in words, so the bar is a picture of the text and not the only way to read it. */
export function BarRows({ rows, description }: Props) {
  const max = Math.max(0, ...rows.map((row) => row.segments.reduce((sum, s) => sum + s.value, 0)));
  const plot = W - LABEL - TEXT;
  const height = rows.length * ROW + 4;
  return (
    <svg className="pui-chart" viewBox={`0 0 ${W} ${height}`} role="img" aria-label={description}>
      <title>{description}</title>
      <line className="pui-chart-axis" x1={LABEL} x2={LABEL} y1={0} y2={height - 4} />
      {rows.map((row, index) => {
        const y = index * ROW + 4;
        let x = LABEL;
        return (
          <g key={row.label}>
            <text className="pui-anchor-end chart-label" x={LABEL - 8} y={y + 16}>
              {shorten(row.label)}
              <title>{row.label}</title>
            </text>
            {row.segments.map((segment, i) => {
              const length = barLength(segment.value, max, plot);
              const rect = length > 0 ? <rect key={i} className={`pui-bar ${segment.cls}`} x={x} y={y + 5} width={length} height={14} rx={2} /> : null;
              x += length;
              return rect;
            })}
            <text x={x + 8} y={y + 16}>
              {row.text}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
