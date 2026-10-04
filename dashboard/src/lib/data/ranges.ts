export const RANGES = { "1h": 3600, "24h": 86_400, "7d": 604_800, "30d": 2_592_000 } as const;
export type Range = keyof typeof RANGES;
export const DEFAULT_RANGE: Range = "24h";

export function parseRange(value: string | null | undefined): Range {
  return value !== null && value !== undefined && Object.hasOwn(RANGES, value) ? (value as Range) : DEFAULT_RANGE;
}

/** In display order. */
export const RANGE_KEYS = Object.keys(RANGES) as Range[];

/** How wide one point of a chart is, so each range has between 60 and 96 points. All divide the day,
 * so a bucket edge is the same instant whatever the range. */
export const BUCKET_SECONDS: Record<Range, number> = { "1h": 60, "24h": 900, "7d": 7200, "30d": 28_800 };
