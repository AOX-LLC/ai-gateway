import { describe, expect, it } from "vitest";
import { LOCKOUT_S, MAX_FAILURES, MAX_LOCKOUT_S, Throttle } from "@/lib/auth/throttle";
import { attemptSignIn } from "@/lib/auth/signin";

describe("the failed sign-in throttle", () => {
  it("locks after the limit, for a minute, then lets one attempt through", () => {
    const throttle = new Throttle();
    for (let i = 0; i < MAX_FAILURES - 1; i++) throttle.failed(100 + i);
    expect(throttle.retryAfter(110)).toBe(0);

    throttle.failed(110);

    expect(throttle.retryAfter(111)).toBe(LOCKOUT_S - 1);
    expect(throttle.retryAfter(110 + LOCKOUT_S)).toBe(0);
  });

  it("lengthens each lockout and keeps the count, so a guesser gets one try per lockout, not five", () => {
    const throttle = new Throttle();
    for (let i = 0; i < MAX_FAILURES; i++) throttle.begin(1000 + i);
    let now = 1000 + MAX_FAILURES;
    const waits: number[] = [];
    for (let round = 0; round < 8; round++) {
      const wait = throttle.retryAfter(now);
      waits.push(wait);
      now += wait; // the lockout ends; one attempt is allowed and starts the next, longer one
      expect(throttle.begin(now)).toBe(0);
      now += 1;
    }

    expect(waits.slice(0, 4)).toEqual([LOCKOUT_S - 1, 2 * LOCKOUT_S - 1, 4 * LOCKOUT_S - 1, 8 * LOCKOUT_S - 1]);
    expect(Math.max(...waits)).toBeLessThanOrEqual(MAX_LOCKOUT_S);
    expect(waits.at(-1)).toBeGreaterThanOrEqual(MAX_LOCKOUT_S - 1);
  });

  it("does not forget old failures with time, so waiting out the lockouts never gives a fresh burst", () => {
    const throttle = new Throttle();
    for (let i = 0; i < MAX_FAILURES; i++) throttle.begin(0);

    // A day, a week later: one attempt each time, then locked again at once. No fresh burst of five.
    for (const later of [24 * 3600, 7 * 24 * 3600]) {
      expect(throttle.begin(later)).toBe(0);
      expect(throttle.begin(later + 1)).toBeGreaterThan(0);
    }
  });

  it("clears the count on a success", () => {
    const throttle = new Throttle();
    for (let i = 0; i < MAX_FAILURES - 1; i++) throttle.failed(100);
    throttle.succeeded();
    for (let i = 0; i < MAX_FAILURES - 1; i++) throttle.failed(101);

    expect(throttle.retryAfter(102)).toBe(0);
  });
});

describe("signing in", () => {
  const secret = "s".repeat(40);
  const base = (verify: (p: string, h: string) => Promise<boolean>, throttle = new Throttle(), now = 1000) => ({
    hash: "scrypt:x",
    secret,
    throttle,
    now: () => now,
    verify,
  });

  it("gives a session for the right password and clears the count", async () => {
    const throttle = new Throttle();
    throttle.failed(1000);

    const result = await attemptSignIn("right", base(async (p) => p === "right", throttle));

    expect(result.status).toBe("ok");
  });

  it("says denied the same way for every wrong or empty or huge password", async () => {
    let asked = 0;
    const verify = async () => {
      asked += 1;
      return false;
    };

    for (const password of ["wrong", "", "x".repeat(5000)]) {
      expect(await attemptSignIn(password, base(verify))).toEqual({ status: "denied" });
    }
    expect(asked).toBe(1); // the empty and the huge password were never even checked
  });

  it("does not look at the password while throttled, even if it is right", async () => {
    const throttle = new Throttle();
    for (let i = 0; i < MAX_FAILURES; i++) throttle.failed(1000);
    expect(throttle.begin(1000)).toBe(LOCKOUT_S);
    let looked = false;

    const result = await attemptSignIn("right", base(async () => (looked = true), throttle));

    expect(result).toEqual({ status: "throttled", retryAfter: LOCKOUT_S });
    expect(looked).toBe(false);
  });

  it("counts guesses sent at the same time, so a burst cannot get past the limit", async () => {
    let checked = 0;
    const slow = async () => {
      checked += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return false;
    };
    const throttle = new Throttle();

    const results = await Promise.all(Array.from({ length: 50 }, () => attemptSignIn("guess", base(slow, throttle))));

    expect(checked).toBe(MAX_FAILURES);
    expect(results.filter((r) => r.status === "denied")).toHaveLength(MAX_FAILURES);
    expect(results.filter((r) => r.status === "throttled")).toHaveLength(50 - MAX_FAILURES);
  });

  it("lets the right password through among the last of the allowed attempts, and clears the count", async () => {
    const throttle = new Throttle();
    for (let i = 0; i < MAX_FAILURES - 2; i++) await attemptSignIn("wrong", base(async () => false, throttle));

    const result = await attemptSignIn("right", base(async (p) => p === "right", throttle));

    expect(result.status).toBe("ok");
    // The count is gone: it takes the full limit again to lock.
    for (let i = 0; i < MAX_FAILURES - 1; i++) expect((await attemptSignIn("wrong", base(async () => false, throttle))).status).toBe("denied");
    expect(throttle.retryAfter(1000)).toBe(0);
  });

  it("lets nobody in when no credential is set", async () => {
    const result = await attemptSignIn("anything", { ...base(async () => true), hash: "" });

    expect(result).toEqual({ status: "disabled" });
  });
});
