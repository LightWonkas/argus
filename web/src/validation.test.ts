// validation.ts — settings form validation (invalid data is caught before it reaches the API).

import { describe, expect, it } from "vitest";
import type { HoldingItemT, WatchlistItemT } from "./api";
import { validateTime, validateWatchHoldings } from "./validation";

function w(symbol: string): WatchlistItemT {
  return { symbol, asset_class: "crypto", tier: "watchlist", note: "" };
}
function h(symbol: string, quantity = 1, avg_cost = 1): HoldingItemT {
  return { symbol, asset_class: "crypto", quantity, avg_cost, cost_currency: "USD", owner: "" };
}

describe("validateWatchHoldings", () => {
  it("valid list → null (no problem)", () => {
    expect(validateWatchHoldings([w("BTCUSDT"), w("XU100")], [h("BTCUSDT")])).toBeNull();
  });

  it("empty symbol → error (no empty row reaches the API)", () => {
    expect(validateWatchHoldings([w("  ")], [])).toContain("empty");
  });

  it("invalid character → error", () => {
    expect(validateWatchHoldings([w("BTC/USDT!")], [])).toContain("invalid");
  });

  it("duplicate symbol → error (watching twice is pointless)", () => {
    expect(validateWatchHoldings([w("BTCUSDT"), w("btcusdt")], [])).toContain("more than once");
  });

  it("negative quantity/cost → error", () => {
    expect(validateWatchHoldings([], [h("BTCUSDT", -1)])).toContain("negative");
    expect(validateWatchHoldings([], [h("BTCUSDT", 1, -5)])).toContain("negative");
  });
});

describe("validateTime (quiet hours HH:MM)", () => {
  it("empty → valid (the quiet window is optional)", () => {
    expect(validateTime(null, "x")).toBeNull();
    expect(validateTime("", "x")).toBeNull();
  });
  it("well-formed HH:MM → valid", () => {
    expect(validateTime("23:00", "x")).toBeNull();
    expect(validateTime("08:30", "x")).toBeNull();
  });
  it("malformed format → error", () => {
    expect(validateTime("25:00", "Quiet start")).toContain("HH:MM");
    expect(validateTime("8:5", "x")).toContain("HH:MM");
    expect(validateTime("abc", "x")).toContain("HH:MM");
  });
});
