export const RANGES = { "1h": 3600, "24h": 86_400, "7d": 604_800, "30d": 2_592_000 } as const;
export type Range = keyof typeof RANGES;
export const DEFAULT_RANGE: Range = "24h";

export function parseRange(value: string | null | undefined): Range {
  return value !== null && value !== undefined && Object.hasOwn(RANGES, value) ? (value as Range) : DEFAULT_RANGE;
}

/** In display order. */
export const RANGE_KEYS = Object.keys(RANGES) as Range[];
