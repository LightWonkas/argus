// Argus dashboard — shared formatting/colour helpers.

export const PENDING = "DATA PENDING";

/** ISO time → Istanbul time (MM/DD, HH:MM). */
export function istanbul(iso: string): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "Europe/Istanbul",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(iso));
}

/** Severity → Tailwind text+border colour (info/low grey · medium amber · high orange · critical red). */
export function severityColor(sev: string): string {
  switch (sev) {
    case "critical":
      return "border-red-500/50 text-red-400 bg-red-500/10";
    case "high":
      return "border-orange-500/50 text-orange-400 bg-orange-500/10";
    case "medium":
      return "border-amber-500/50 text-amber-400 bg-amber-500/10";
    default:
      return "border-zinc-600 text-zinc-400 bg-zinc-700/30";
  }
}

/** Evidence tier → colour ([A] green · [B] blue · [C] grey). */
export function evidenceColor(tier: string): string {
  switch (tier) {
    case "A":
      return "bg-emerald-500/20 text-emerald-400";
    case "B":
      return "bg-sky-500/20 text-sky-400";
    default:
      return "bg-zinc-600/30 text-zinc-400";
  }
}

/** Direction → colour + arrow (colour-blind friendly: there is an arrow too). */
export function biasBadge(bias: string | null): { label: string; cls: string } {
  if (bias === "bullish") return { label: "▲ bullish", cls: "text-emerald-400" };
  if (bias === "bearish") return { label: "▼ bearish", cls: "text-red-400" };
  if (bias === "neutral") return { label: "● neutral", cls: "text-zinc-400" };
  return { label: "—", cls: "text-zinc-500" };
}

/** Freshness → dot colour (fresh green · stale amber · no data red). */
export function freshnessDot(f: string): string {
  if (f === "fresh") return "bg-emerald-500";
  if (f === "stale") return "bg-amber-500";
  return "bg-red-500/70";
}

// BIST/metal (ETFs traded on BIST) trade on weekdays only; crypto/FX run 24/7.
const MARKET_CLOSES_WEEKENDS = new Set(["bist", "metal", "equity"]);

/** Rough human-readable duration between two dates. */
function humanAge(ms: number): string {
  const min = Math.round(ms / 60_000);
  if (min < 60) return `${min} min`;
  const hr = Math.round(min / 60);
  if (hr < 48) return `${hr} h`;
  return `${Math.round(hr / 24)} d`;
}

/**
 * Explanatory tooltip text for the freshness badge — answers "why is it stale?".
 *
 * An amber dot does not always mean "bad data". BIST/metal markets are closed on
 * weekends, so Friday's close is the last valid candle until Monday morning — this is
 * explained as "market closed" rather than labelled "stale".
 * Crypto trades 24/7, so a real delay there is always staleness.
 */
export function freshnessTitle(
  freshness: string,
  barTime: string | null,
  assetClass: string,
  now: Date = new Date(),
): string {
  if (freshness === "none" || !barTime) {
    return "DATA PENDING — no candles for this symbol yet (waiting for the feed)";
  }
  const bar = new Date(barTime);
  const ageMs = now.getTime() - bar.getTime();
  const stamp = istanbul(barTime);
  if (freshness === "fresh") {
    return `Fresh · last candle ${stamp}`;
  }
  // stale: genuinely delayed, or is the market closed?
  const weekendMarket = MARKET_CLOSES_WEEKENDS.has(assetClass);
  const nowDay = now.getDay(); // 0=Sun, 6=Sat
  const isWeekendNow = nowDay === 0 || nowDay === 6;
  const isMondayEarly = nowDay === 1; // Mon — the daily candle may not have closed yet
  if (weekendMarket && (isWeekendNow || isMondayEarly)) {
    return `Market closed — last traded candle ${stamp}. The exchange is closed on weekends, so this is the latest data, not a feed delay.`;
  }
  return `Stale · last candle ${stamp} (${humanAge(ageMs)} ago) — the feed may have stopped`;
}

const PENDING_CELL = "—";

/** Number → fixed digits (null renders a short dash instead of DATA PENDING). */
export function num(v: number | null, digits = 2): string {
  return v == null ? PENDING_CELL : v.toFixed(digits);
}

/** Percent change → coloured (+ green / − red, with an arrow). */
export function changeCell(v: number | null): { text: string; cls: string } {
  if (v == null) return { text: PENDING_CELL, cls: "text-zinc-500" };
  const arrow = v > 0 ? "▲" : v < 0 ? "▼" : "·";
  const cls = v > 0 ? "text-emerald-400" : v < 0 ? "text-red-400" : "text-zinc-400";
  return { text: `${arrow} ${v >= 0 ? "+" : ""}${v.toFixed(2)}%`, cls };
}

/** RSI → colour (≥70 overbought red · ≤30 oversold green · neutral in between). */
export function rsiCls(v: number | null): string {
  if (v == null) return "text-zinc-500";
  if (v >= 70) return "text-red-400";
  if (v <= 30) return "text-emerald-400";
  return "text-zinc-300";
}
