// format.ts — formatting/colour helper tests (honesty: null → dash, nothing invented).

import { describe, expect, it } from "vitest";
import {
  biasBadge,
  changeCell,
  evidenceColor,
  freshnessDot,
  freshnessTitle,
  num,
  PENDING,
  rsiCls,
  severityColor,
} from "./format";

describe("num", () => {
  it("null → dash (no invented zero)", () => {
    expect(num(null)).toBe("—");
  });
  it("rounds a number to fixed digits", () => {
    expect(num(3.14159, 2)).toBe("3.14");
    expect(num(10, 0)).toBe("10");
  });
});

describe("changeCell", () => {
  it("null → dash, neutral colour", () => {
    expect(changeCell(null)).toEqual({ text: "—", cls: "text-zinc-500" });
  });
  it("positive → up arrow + green + sign", () => {
    const r = changeCell(2.5);
    expect(r.text).toBe("▲ +2.50%");
    expect(r.cls).toContain("emerald");
  });
  it("negative → down arrow + red", () => {
    const r = changeCell(-1.2);
    expect(r.text).toBe("▼ -1.20%");
    expect(r.cls).toContain("red");
  });
});

describe("biasBadge", () => {
  it("bullish/bearish/neutral → with an arrow (colour-blind friendly)", () => {
    expect(biasBadge("bullish").label).toContain("▲");
    expect(biasBadge("bearish").label).toContain("▼");
    expect(biasBadge("neutral").label).toContain("●");
  });
  it("unknown/null → dash", () => {
    expect(biasBadge(null).label).toBe("—");
    expect(biasBadge("xyz").label).toBe("—");
  });
});

describe("rsiCls", () => {
  it("≥70 red (overbought), ≤30 green (oversold), neutral in between", () => {
    expect(rsiCls(75)).toContain("red");
    expect(rsiCls(25)).toContain("emerald");
    expect(rsiCls(50)).toContain("zinc-300");
  });
  it("null → muted", () => {
    expect(rsiCls(null)).toContain("zinc-500");
  });
});

describe("severityColor / evidenceColor / freshnessDot", () => {
  it("severity levels have distinct colours", () => {
    expect(severityColor("critical")).toContain("red");
    expect(severityColor("high")).toContain("orange");
    expect(severityColor("medium")).toContain("amber");
    expect(severityColor("info")).toContain("zinc");
  });
  it("evidence tier A>B>C colours", () => {
    expect(evidenceColor("A")).toContain("emerald");
    expect(evidenceColor("B")).toContain("sky");
    expect(evidenceColor("C")).toContain("zinc");
  });
  it("freshness: fresh green, stale amber, none red", () => {
    expect(freshnessDot("fresh")).toContain("emerald");
    expect(freshnessDot("stale")).toContain("amber");
    expect(freshnessDot("none")).toContain("red");
  });
});

describe("PENDING constant", () => {
  it("is the honesty placeholder", () => {
    expect(PENDING).toBe("DATA PENDING");
  });
});

describe("freshnessTitle (why stale? — weekend awareness)", () => {
  // Fixed "now" values: Monday 2026-06-15 09:00 Istanbul; Saturday 2026-06-13; Wednesday 2026-06-17.
  const monday = new Date("2026-06-15T06:00:00Z");
  const saturday = new Date("2026-06-13T10:00:00Z");
  const wednesday = new Date("2026-06-17T10:00:00Z");
  const fridayBar = "2026-06-12T06:00:00Z"; // Friday closing candle

  it("explicit DATA PENDING when there is no data (nothing invented)", () => {
    expect(freshnessTitle("none", null, "bist", monday)).toContain("DATA PENDING");
  });

  it("fresh → fresh + last candle time", () => {
    expect(freshnessTitle("fresh", fridayBar, "crypto", monday)).toContain("Fresh");
  });

  it("BIST + Monday morning + Friday candle → 'market closed' (NOT labelled stale)", () => {
    const t = freshnessTitle("stale", fridayBar, "bist", monday);
    expect(t).toContain("Market closed");
    expect(t).not.toContain("Stale");
  });

  it("metal (BIST ETF) on a weekend → market closed", () => {
    expect(freshnessTitle("stale", fridayBar, "metal", saturday)).toContain("Market closed");
  });

  it("BIST + midweek genuine delay → 'Stale' (feed stopped)", () => {
    const t = freshnessTitle("stale", fridayBar, "bist", wednesday);
    expect(t).toContain("Stale");
  });

  it("crypto (24/7) → NO weekend excuse, a delay is always stale", () => {
    const t = freshnessTitle("stale", fridayBar, "crypto", saturday);
    expect(t).toContain("Stale");
  });
});
