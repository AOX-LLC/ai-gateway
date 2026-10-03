import { describe, expect, it } from "vitest";
import { clock, dayAndMinute, formatInt, formatMs, formatPercent, formatRate, windowLabel, words } from "@/lib/format";

describe("formatting", () => {
  it("rounds to what a person would say", () => {
    expect(formatInt(1284.4)).toBe("1,284");
    expect(formatMs(41.5)).toBe("42");
    expect(formatMs(0.84)).toBe("0.8");
    expect(formatPercent(0.9)).toBe("90.0");
    expect(formatRate(0.4)).toBe("0.4");
    expect(formatRate(184.2)).toBe("184");
  });

  it("reads times as UTC and does not depend on the machine's zone", () => {
    expect(clock("2026-10-03T20:49:45.319663Z")).toBe("20:49:45");
    expect(dayAndMinute("2026-10-03T20:49:45.319663Z")).toBe("Oct 3, 20:49");
    expect(dayAndMinute("not a date")).toBe("not a date");
  });

  it("names the window in the unit a person uses", () => {
    expect(windowLabel(3600)).toBe("60 minutes");
    expect(windowLabel(86_400)).toBe("24 hours");
    expect(windowLabel(604_800)).toBe("7 days");
  });

  it("turns a layer or code into words", () => {
    expect(words("rate_limit")).toBe("Rate limit");
    expect(words("approval_pending")).toBe("Approval pending");
  });
});
