import { describe, expect, it } from "vitest";
import { barLength, linePath, niceAxis, timeTicks } from "@/components/charts/scale";

describe("the axis", () => {
  it("rounds the top up to a round number and counts up to it from zero", () => {
    expect(niceAxis(37)).toEqual({ max: 40, ticks: [0, 10, 20, 30, 40] });
    expect(niceAxis(1213)).toEqual({ max: 1500, ticks: [0, 500, 1000, 1500] });
    expect(niceAxis(0.8).max).toBeCloseTo(0.8, 5);
    expect(niceAxis(8).ticks[0]).toBe(0);
  });

  it("is a unit axis when there is nothing to show", () => {
    expect(niceAxis(0)).toEqual({ max: 1, ticks: [0, 1] });
    expect(niceAxis(Number.NaN).max).toBe(1);
    expect(niceAxis(-5).max).toBe(1);
  });
});

describe("the time ticks", () => {
  const hour = 3600;
  const start = Date.UTC(2026, 9, 3, 10, 0, 0) / 1000;

  it("label a day-long window with the time and a week-long one with the day", () => {
    const day = timeTicks(start, start + 24 * hour);
    const week = timeTicks(start, start + 7 * 24 * hour);

    expect(day.length).toBeGreaterThanOrEqual(3);
    expect(day[0]?.label).toMatch(/^\d{2}:\d{2}$/);
    expect(week[0]?.label).toMatch(/^[A-Z][a-z]{2} \d{1,2}$/);
    expect(day.every((tick) => tick.at >= start && tick.at <= start + 24 * hour)).toBe(true);
  });

  it("are empty for an empty or backwards window", () => {
    expect(timeTicks(start, start)).toEqual([]);
    expect(timeTicks(start, start - 1)).toEqual([]);
  });
});

describe("the line", () => {
  const x = (value: number) => value * 10;
  const y = (value: number) => 100 - value;

  it("breaks where a value is missing instead of drawing across the gap", () => {
    expect(linePath([[0, 1], [1, 2], [2, null], [3, 4]], x, y)).toBe("M0.0 99.0L10.0 98.0M30.0 96.0");
    expect(linePath([[0, null]], x, y)).toBe("");
  });
});

describe("a bar", () => {
  it("is a share of the maximum and never leaves the track", () => {
    expect(barLength(5, 10, 200)).toBe(100);
    expect(barLength(50, 10, 200)).toBe(200);
    expect(barLength(-1, 10, 200)).toBe(0);
    expect(barLength(5, 0, 200)).toBe(0);
  });
});
