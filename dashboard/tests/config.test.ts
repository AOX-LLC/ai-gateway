import { describe, expect, it } from "vitest";
import { ConfigError, parseConfig } from "@/lib/config";

const good = {
  DASHBOARD_DATABASE_URL: "postgresql://telemetry_reader:x@postgres:5432/db",
  DASHBOARD_SESSION_SECRET: "k".repeat(40),
};

describe("the dashboard's settings", () => {
  it("accepts a complete set, with sign-in off until a credential is set", () => {
    expect(parseConfig(good)).toMatchObject({ adminPasswordHash: "", sampleData: false });
    expect(parseConfig({ ...good, DASHBOARD_SAMPLE_DATA: "1" }).sampleData).toBe(true);
  });

  it.each([
    ["no database", { ...good, DASHBOARD_DATABASE_URL: "" }],
    ["no secret", { ...good, DASHBOARD_SESSION_SECRET: "" }],
    ["a short secret", { ...good, DASHBOARD_SESSION_SECRET: "short" }],
    ["a placeholder secret", { ...good, DASHBOARD_SESSION_SECRET: `change-me-${"x".repeat(30)}` }],
  ])("stops for %s", (_name, env) => {
    expect(() => parseConfig(env)).toThrow(ConfigError);
  });
});
