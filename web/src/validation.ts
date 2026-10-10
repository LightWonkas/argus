// Settings form validation — invalid data is caught before it reaches the API (empty symbol,
// negative quantity, malformed time). Messages are actionable; stops at the first error.

import type { HoldingItemT, WatchlistItemT } from "./api";

// Symbol: letters/digits/dot/dash, 1–20 characters (e.g. BTCUSDT, XU100, GC=F).
const SYMBOL_RE = /^[A-Za-z0-9.\-=]{1,20}$/;

/** Validate watchlist + holdings. Returns the first problem (null if none). */
export function validateWatchHoldings(
  watchlist: WatchlistItemT[],
  holdings: HoldingItemT[],
): string | null {
  const seen = new Set<string>();
  for (const [i, w] of watchlist.entries()) {
    const sym = w.symbol.trim().toUpperCase();
    if (!sym) return `Watchlist #${i + 1}: symbol cannot be empty`;
    if (!SYMBOL_RE.test(sym)) return `Watchlist: "${w.symbol}" is an invalid symbol (letters/digits)`;
    if (seen.has(sym)) return `Watchlist: "${sym}" was added more than once`;
    seen.add(sym);
  }
  for (const [i, h] of holdings.entries()) {
    const sym = h.symbol.trim().toUpperCase();
    if (!sym) return `Holding #${i + 1}: symbol cannot be empty`;
    if (!SYMBOL_RE.test(sym)) return `Holding: "${h.symbol}" is an invalid symbol`;
    if (h.quantity < 0) return `Holding "${sym}": quantity cannot be negative`;
    if (h.avg_cost < 0) return `Holding "${sym}": cost cannot be negative`;
  }
  return null;
}

// Quiet hours window: HH:MM (00:00–23:59) or empty.
const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Validate a single HH:MM field (empty = valid). */
export function validateTime(value: string | null, fieldLabel: string): string | null {
  if (value == null || value === "") return null;
  if (!TIME_RE.test(value)) return `${fieldLabel}: time must be in HH:MM format (e.g. 23:00)`;
  return null;
}
