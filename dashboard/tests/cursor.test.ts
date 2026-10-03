import { describe, expect, it } from "vitest";
import { decodeCursor, encodeCursor } from "@/lib/data/cursor";
import { DEFAULT_RANGE, parseRange } from "@/lib/data/ranges";

const cursor = { ts: "2026-10-03T20:49:45.319663Z", id: "f35513de-f746-420a-828b-8756e82229f9" };

describe("the page cursor", () => {
  it("keeps the time to the microsecond", () => {
    expect(decodeCursor(encodeCursor(cursor))).toEqual(cursor);
  });

  it.each([
    [null],
    [""],
    ["not base64 json"],
    [Buffer.from("[1,2]").toString("base64url")],
    [Buffer.from(JSON.stringify({ ts: "yesterday", id: cursor.id })).toString("base64url")],
    [Buffer.from(JSON.stringify({ ts: cursor.ts, id: "1; DROP TABLE x" })).toString("base64url")],
    [Buffer.from(JSON.stringify({ ts: cursor.ts, id: cursor.id, extra: 1 })).toString("base64url").repeat(10)],
  ])("is dropped when it is not one the dashboard made: %s", (value) => {
    expect(decodeCursor(value)).toBeUndefined();
  });
});

describe("the time range", () => {
  it("takes the four ranges and falls back for anything else", () => {
    expect(parseRange("1h")).toBe("1h");
    expect(parseRange("30d")).toBe("30d");
    for (const value of ["", "1y", "constructor", "__proto__", null, undefined]) {
      expect(parseRange(value)).toBe(DEFAULT_RANGE);
    }
  });
});
