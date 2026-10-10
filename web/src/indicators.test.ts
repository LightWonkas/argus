// indicators.ts — client-side EMA/RSI pure function tests (no network, deterministic).

import { describe, expect, it } from "vitest";
import { ema, rsi } from "./indicators";

describe("ema", () => {
  it("returns null before warm-up (up to period-1 points)", () => {
    const out = ema([1, 2, 3, 4, 5], 3);
    expect(out[0]).toBeNull();
    expect(out[1]).toBeNull();
    // 3rd point (index 2) is the first SMA seed: (1+2+3)/3 = 2
    expect(out[2]).toBeCloseTo(2, 6);
  });

  it("applies the EMA formula after the seed", () => {
    const out = ema([1, 2, 3, 4, 5], 3);
    const k = 2 / (3 + 1); // 0.5
    const seed = 2; // (1+2+3)/3
    expect(out[3]).toBeCloseTo(4 * k + seed * (1 - k), 6); // 3
    expect(out[4]).toBeCloseTo(5 * k + (out[3] as number) * (1 - k), 6);
  });

  it("constant series → EMA equals that constant", () => {
    const out = ema([7, 7, 7, 7, 7], 3);
    expect(out[4]).toBeCloseTo(7, 6);
  });
});

describe("rsi", () => {
  it("all null with insufficient data (length <= period)", () => {
    const out = rsi([1, 2, 3], 14);
    expect(out.every((v) => v === null)).toBe(true);
  });

  it("strictly rising series → RSI 100 (no losses)", () => {
    const closes = Array.from({ length: 20 }, (_, i) => 100 + i);
    const out = rsi(closes, 14);
    expect(out[14]).toBe(100);
  });

  it("warm-up window is null, then a value within 0–100", () => {
    const closes = [44, 44.3, 44.1, 44.5, 43.9, 44.6, 44.2, 45, 44.8, 45.2, 45, 45.5, 45.1, 45.8, 45.4, 46];
    const out = rsi(closes, 14);
    expect(out[13]).toBeNull(); // warm-up
    const v = out[14];
    expect(v).not.toBeNull();
    expect(v as number).toBeGreaterThanOrEqual(0);
    expect(v as number).toBeLessThanOrEqual(100);
  });
});
