/** The arithmetic of a chart, kept apart from the drawing so it can be tested: nice axis limits,
 * time ticks, and a line path that breaks where there is no value. */

/** An upper limit for the axis and the ticks up to it: 0 to a round number, in about four steps. */
export function niceAxis(max: number, steps = 4, whole = false): { max: number; ticks: number[] } {
  if (!(max > 0)) return { max: 1, ticks: [0, 1] };
  const rough = max / steps;
  const power = 10 ** Math.floor(Math.log10(rough));
  const niceUnit = [1, 2, 2.5, 5, 10].map((m) => m * power).find((candidate) => candidate >= rough) ?? 10 * power;
  // Counts of things are whole numbers: never label an axis 0, 0.25, 0.5.
  const unit = whole ? Math.max(1, Math.ceil(niceUnit)) : niceUnit;
  const top = Math.ceil(max / unit) * unit;
  const ticks: number[] = [];
  for (let value = 0; value <= top + unit / 2; value += unit) ticks.push(Number(value.toFixed(10)));
  return { max: top, ticks };
}

export type TimeTick = { at: number; label: string };

const pad = (n: number) => String(n).padStart(2, "0");
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** About `count` evenly spaced ticks between two instants (epoch seconds, UTC). A span under two days
 * is labelled with the time of day, a longer one with the day. */
export function timeTicks(start: number, end: number, count = 5): TimeTick[] {
  const span = end - start;
  if (!(span > 0)) return [];
  const day = span >= 2 * 86_400;
  const steps = [60, 300, 600, 900, 1800, 3600, 7200, 10_800, 21_600, 43_200, 86_400, 172_800];
  const wanted = span / count;
  const step = steps.find((candidate) => candidate >= wanted) ?? 172_800;
  const ticks: TimeTick[] = [];
  for (let at = Math.ceil(start / step) * step; at <= end; at += step) {
    const date = new Date(at * 1000);
    ticks.push({ at, label: day ? `${MONTHS[date.getUTCMonth()]} ${date.getUTCDate()}` : `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}` });
  }
  return ticks;
}

/** An SVG path through the points, started again after each missing value, so a gap is a gap. */
export function linePath(points: readonly (readonly [number, number | null])[], toX: (x: number) => number, toY: (y: number) => number): string {
  let path = "";
  let drawing = false;
  for (const [x, y] of points) {
    if (y === null) {
      drawing = false;
      continue;
    }
    path += `${drawing ? "L" : "M"}${toX(x).toFixed(1)} ${toY(y).toFixed(1)}`;
    drawing = true;
  }
  return path;
}

/** The share of the maximum, as a length: never negative, never past the maximum. */
export function barLength(value: number, max: number, full: number): number {
  return max > 0 ? Math.min(Math.max(value / max, 0), 1) * full : 0;
}

/** Where to stop the axis when one spike would flatten everything else, or null when the data has no
 * such spike. The ceiling is three times the 95th-percentile value; the chart must label what it cuts. */
export function robustCeiling(values: readonly number[]): number | null {
  const sorted = values.filter((v) => v > 0).sort((a, b) => a - b);
  if (sorted.length < 10) return null;
  const typical = sorted[Math.floor(sorted.length * 0.95)] ?? 0;
  const ceiling = typical * 3;
  return (sorted.at(-1) ?? 0) > ceiling * 1.5 ? ceiling : null;
}
