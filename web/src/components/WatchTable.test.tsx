// WatchTable render smoke test — the api is mocked (no network). Honesty: no data → DATA PENDING.

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { QuoteItem } from "../api";
import { WatchTable } from "./WatchTable";

// Mock the api module — no real network/proxy calls.
vi.mock("../api", () => ({
  api: {
    quotes: vi.fn(),
    // Sparklines load with a single batch call; empty response → no line is drawn (—).
    ohlcvBatch: vi.fn().mockResolvedValue({ items: [] }),
  },
}));

import { api } from "../api";
const mockedQuotes = vi.mocked(api.quotes);

afterEach(() => {
  vi.clearAllMocks();
});

const row = (over: Partial<QuoteItem>): QuoteItem => ({
  symbol: "BTCUSDT",
  asset_class: "crypto",
  tier: "watchlist",
  price: null,
  change_pct_24h: null,
  rsi: null,
  macd: null,
  macd_signal: null,
  atr: null,
  freshness: "none",
  bar_time: null,
  bar_count: 0,
  timeframes: ["1d"],
  ...over,
});

describe("WatchTable", () => {
  it("honestly shows DATA PENDING for an empty watchlist (no fabricated rows)", async () => {
    mockedQuotes.mockResolvedValue([]);
    render(<WatchTable />);
    await waitFor(() => {
      expect(screen.getByText(/DATA PENDING/)).toBeInTheDocument();
    });
  });

  it("shows the symbol row and the real price once data arrives", async () => {
    mockedQuotes.mockResolvedValue([
      row({ symbol: "BTCUSDT", price: 72923.19, rsi: 55, freshness: "fresh" }),
    ]);
    render(<WatchTable />);
    await waitFor(() => {
      expect(screen.getByText("BTCUSDT")).toBeInTheDocument();
    });
    // The price is formatted to 2 decimals (num()).
    expect(screen.getByText("72923.19")).toBeInTheDocument();
  });

  it("shows a dash (—) instead of a fabricated value for a symbol with no price (no data)", async () => {
    mockedQuotes.mockResolvedValue([row({ symbol: "XU100", price: null, freshness: "none" })]);
    render(<WatchTable />);
    await waitFor(() => {
      expect(screen.getByText("XU100")).toBeInTheDocument();
    });
    // price null → "—" (table cell); there must be at least one dash.
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
