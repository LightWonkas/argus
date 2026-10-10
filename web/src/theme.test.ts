import { describe, expect, it } from "vitest";
import { deltaTone, tone, usageBarColor } from "./theme";

describe("usageBarColor", () => {
  it("green at low usage", () => {
    expect(usageBarColor(0)).toBe("bg-emerald-500");
    expect(usageBarColor(0.59)).toBe("bg-emerald-500");
  });
  it("amber from 0.6", () => {
    expect(usageBarColor(0.6)).toBe("bg-amber-500");
    expect(usageBarColor(0.89)).toBe("bg-amber-500");
  });
  it("red from 0.9", () => {
    expect(usageBarColor(0.9)).toBe("bg-rose-500");
    expect(usageBarColor(1.5)).toBe("bg-rose-500");
  });
});

describe("deltaTone", () => {
  it("null/undefined → pending tone (no invented colour)", () => {
    expect(deltaTone(null)).toBe(tone.pending);
    expect(deltaTone(undefined)).toBe(tone.pending);
  });
  it("zero and positive → positive tone", () => {
    expect(deltaTone(0)).toBe(tone.positive);
    expect(deltaTone(12.5)).toBe(tone.positive);
  });
  it("negative → negative tone", () => {
    expect(deltaTone(-3)).toBe(tone.negative);
  });
});
