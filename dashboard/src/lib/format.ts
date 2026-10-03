/** Plain-words formatting for the panels. Times are UTC and say so; numbers are rounded to what a
 * person would say, with units. Fixed to en-US so the server and the browser print the same text. */

const integer = new Intl.NumberFormat("en-US");
const oneDecimal = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1, minimumFractionDigits: 1 });

export const formatInt = (value: number): string => integer.format(Math.round(value));

/** 41.5 -> "42", 0.8 -> "0.8": under 10 keeps a decimal, above does not. */
export function formatMs(value: number): string {
  return value < 10 ? oneDecimal.format(value) : integer.format(Math.round(value));
}

export const formatPercent = (fraction: number): string => `${oneDecimal.format(fraction * 100)}`;

/** Requests per minute: "0.4", "2.1", "184". */
export function formatRate(perMinute: number): string {
  return perMinute < 10 ? oneDecimal.format(perMinute) : integer.format(Math.round(perMinute));
}

/** "2026-10-03T20:49:45.319663Z" -> "20:49:45". */
export const clock = (iso: string): string => iso.slice(11, 19);

/** "2026-10-03T20:49:45.319663Z" -> "Oct 3, 20:49". */
export function dayAndMinute(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const day = date.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  return `${day}, ${iso.slice(11, 16)}`;
}

export function windowLabel(seconds: number): string {
  if (seconds < 7200) return `${Math.round(seconds / 60)} minutes`;
  if (seconds < 172_800) return `${Math.round(seconds / 3600)} hours`;
  return `${Math.round(seconds / 86_400)} days`;
}

/** A layer or deny code as words: "rate_limit" -> "Rate limit". */
export function words(code: string): string {
  const text = code.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}
