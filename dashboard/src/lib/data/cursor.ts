/** A page of decisions is found by where the last one ended: (time, request id), both read off the
 * last row, so a row written while someone pages is never skipped or shown twice. It is opaque to the
 * client and checked here, because it comes back from the client. */

const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export type Cursor = { ts: string; id: string };

export function encodeCursor(cursor: Cursor): string {
  return Buffer.from(JSON.stringify(cursor)).toString("base64url");
}

export function decodeCursor(encoded: string | null | undefined): Cursor | undefined {
  if (!encoded || encoded.length > 200) return undefined;
  try {
    const value: unknown = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
    if (typeof value !== "object" || value === null) return undefined;
    const { ts, id } = value as Record<string, unknown>;
    if (typeof ts !== "string" || typeof id !== "string") return undefined;
    if (!ISO.test(ts) || !UUID.test(id) || Number.isNaN(Date.parse(ts))) return undefined;
    return { ts, id };
  } catch {
    return undefined;
  }
}
