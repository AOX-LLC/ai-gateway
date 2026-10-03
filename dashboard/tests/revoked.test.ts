import { describe, expect, it } from "vitest";
import { isRevoked, revoke } from "@/lib/auth/revoked";
import { ABSOLUTE_LIFETIME_S } from "@/lib/auth/session";

describe("signed-out sessions", () => {
  it("are remembered, and forgotten once they could no longer be used anyway", () => {
    revoke("session-a", 1000);
    expect(isRevoked("session-a")).toBe(true);
    expect(isRevoked("session-b")).toBe(false);

    revoke("session-c", 1000 + ABSOLUTE_LIFETIME_S + 120); // makes the first one's time pass
    expect(isRevoked("session-a")).toBe(false);
    expect(isRevoked("session-c")).toBe(true);
  });

  it("are kept on globalThis, so a separately bundled route handler sees what the proxy sees", () => {
    revoke("session-shared", 5000);
    const holder = globalThis as unknown as Record<symbol, Map<string, number> | undefined>;

    expect(holder[Symbol.for("ai-gateway.dashboard.revoked-sessions")]?.has("session-shared")).toBe(true);
  });
});
