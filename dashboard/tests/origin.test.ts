import { describe, expect, it } from "vitest";
import { isSameOrigin } from "@/lib/auth/origin";

const headers = (values: Record<string, string>) => new Headers(values);

describe("the same-origin check", () => {
  it("accepts a request from the page's own host", () => {
    expect(isSameOrigin(headers({ host: "127.0.0.1:4400", origin: "http://127.0.0.1:4400" }))).toBe(true);
    expect(isSameOrigin(headers({ host: "127.0.0.1:4400", origin: "http://127.0.0.1:4400", "sec-fetch-site": "same-origin" }))).toBe(true);
  });

  it.each([
    ["no origin", { host: "127.0.0.1:4400" }],
    ["another site", { host: "127.0.0.1:4400", origin: "https://evil.example" }],
    ["another port", { host: "127.0.0.1:4400", origin: "http://127.0.0.1:9999" }],
    ["null origin", { host: "127.0.0.1:4400", origin: "null" }],
    ["cross-site fetch", { host: "127.0.0.1:4400", origin: "http://127.0.0.1:4400", "sec-fetch-site": "cross-site" }],
    ["no host", { origin: "http://127.0.0.1:4400" }],
  ])("refuses %s", (_name, values) => {
    expect(isSameOrigin(headers(values))).toBe(false);
  });
});
