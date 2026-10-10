import { describe, expect, it } from "vitest";
import type { AlertItem } from "./api";
import { titleWithBadge, unseenCritical } from "./alertBadge";

function alert(over: Partial<AlertItem>): AlertItem {
  return {
    analysis_id: 1,
    created_at: "2026-06-12T10:00:00+00:00",
    symbol: "XU100",
    severity: "critical",
    kind: "rsi",
    message: "test",
    ...over,
  };
}

describe("titleWithBadge", () => {
  it("bare title when there is no badge", () => {
    expect(titleWithBadge(0)).toBe("Argus Cockpit");
  });
  it("(n) prefix when there are unread criticals", () => {
    expect(titleWithBadge(2)).toBe("(2) Argus Cockpit");
  });
});

describe("unseenCritical", () => {
  it("only critical severity counts", () => {
    const list = [alert({ severity: "critical" }), alert({ severity: "high" })];
    expect(unseenCritical(list, null)).toHaveLength(1);
  });
  it("counts alerts after seenAt, not the earlier ones", () => {
    const list = [
      alert({ created_at: "2026-06-12T09:00:00+00:00" }),
      alert({ created_at: "2026-06-12T11:00:00+00:00" }),
    ];
    expect(unseenCritical(list, "2026-06-12T10:00:00+00:00")).toHaveLength(1);
  });
  it("counts every critical when seenAt is missing", () => {
    expect(unseenCritical([alert({})], null)).toHaveLength(1);
  });
});
