import { useEffect, useMemo, useState } from "react";
import { api, type QuoteItem } from "../api";
import { PENDING, changeCell, freshnessDot, freshnessTitle, num, rsiCls } from "../format";
import { Sparkline } from "./Sparkline";

const ASSET_LABEL: Record<string, string> = {
  bist: "BIST",
  metal: "Metal",
  crypto: "Crypto",
  fx: "FX",
  equity: "Equity",
};

// Sortable columns — in numeric fields nulls always sort last (no fabricated zeros).
type SortKey =
  | "symbol"
  | "asset_class"
  | "price"
  | "change_pct_24h"
  | "rsi"
  | "macd"
  | "atr"
  | "freshness";

const FRESH_RANK: Record<string, number> = { fresh: 2, stale: 1, none: 0 };

/** Watchlist/holdings table: trend · price · 24h% · RSI · MACD · ATR · freshness.
 *  Click a header to sort; click a row for `onSelect(symbol)` → the chart/quote follow. */
export function WatchTable({ onSelect }: { onSelect?: (symbol: string) => void }) {
  const [quotes, setQuotes] = useState<QuoteItem[] | null>(null);
  const [spark, setSpark] = useState<Record<string, number[]>>({});
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  useEffect(() => {
    let active = true;
    const load = () =>
      api
        .quotes()
        .then((q) => active && setQuotes(q))
        .catch(() => active && setQuotes([]));
    void load();
    const t = setInterval(load, 15_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  // Sparkline series: the last ~40 closes for all symbols in ONE request (/api/ohlcv-batch),
  // instead of one call per symbol. Refreshed less often than quotes (60 s); refetched when
  // the symbol set changes.
  const symbolsKey = useMemo(
    () => (quotes ?? []).map((q) => q.symbol).join(","),
    [quotes],
  );
  useEffect(() => {
    if (!symbolsKey) return;
    const syms = symbolsKey.split(",");
    let active = true;
    const load = () => {
      api
        .ohlcvBatch(syms)
        .then((d) => {
          if (!active) return;
          const next: Record<string, number[]> = {};
          for (const item of d.items) next[item.symbol] = item.closes;
          setSpark(next); // symbols without data are absent from the response → no sparkline (—)
        })
        .catch(() => {
          /* network/access error → sparklines stay empty (— is shown) */
        });
    };
    load();
    const t = setInterval(load, 60_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [symbolsKey]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "symbol" || key === "asset_class" ? "asc" : "desc");
    }
  }

  const rows = useMemo(() => {
    if (!quotes) return null;
    if (!sortKey) return quotes;
    const dir = sortDir === "asc" ? 1 : -1;
    const sorted = [...quotes].sort((a, b) => {
      if (sortKey === "symbol" || sortKey === "asset_class") {
        return a[sortKey].localeCompare(b[sortKey], "en") * dir;
      }
      if (sortKey === "freshness") {
        return (FRESH_RANK[a.freshness] - FRESH_RANK[b.freshness]) * dir;
      }
      const av = a[sortKey];
      const bv = b[sortKey];
      // nulls go last regardless of direction
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      return (av - bv) * dir;
    });
    return sorted;
  }, [quotes, sortKey, sortDir]);

  const arrow = (key: SortKey) =>
    sortKey === key ? (sortDir === "asc" ? " ▲" : " ▼") : "";

  const Th = ({
    label,
    sk,
    align = "right",
  }: {
    label: string;
    sk: SortKey;
    align?: "left" | "right" | "center";
  }) => (
    <th className={`px-3 font-medium ${align === "left" ? "text-left" : align === "center" ? "text-center" : "text-right"}`}>
      <button
        onClick={() => toggleSort(sk)}
        className={`hover:text-zinc-200 ${sortKey === sk ? "text-zinc-200" : ""}`}
      >
        {label}
        <span className="text-[10px]">{arrow(sk)}</span>
      </button>
    </th>
  );

  return (
    <div className="p-3">
      {rows === null ? (
        <p className="text-zinc-500">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-zinc-500">{PENDING} — the watchlist is empty</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-left text-xs text-zinc-500">
                <Th label="Asset" sk="symbol" align="left" />
                <th className="px-3 text-center font-medium">Trend</th>
                <Th label="Class" sk="asset_class" align="left" />
                <Th label="Price" sk="price" />
                <Th label="24h" sk="change_pct_24h" />
                <Th label="RSI" sk="rsi" />
                <Th label="MACD" sk="macd" />
                <Th label="ATR" sk="atr" />
                <Th label="Freshness" sk="freshness" align="center" />
              </tr>
            </thead>
            <tbody>
              {rows.map((q) => {
                const chg = changeCell(q.change_pct_24h);
                const noData = q.freshness === "none";
                const series = spark[q.symbol];
                return (
                  <tr
                    key={q.symbol}
                    onClick={() => onSelect?.(q.symbol)}
                    className={`border-b border-zinc-800/60 ${noData ? "opacity-50" : ""} ${
                      onSelect ? "cursor-pointer hover:bg-zinc-800/40" : ""
                    }`}
                  >
                    <td className="py-2 pr-3 font-medium text-zinc-200">{q.symbol}</td>
                    <td className="px-3 py-1">
                      <div className="flex justify-center">
                        {series ? <Sparkline values={series} /> : <span className="text-xs text-zinc-500">—</span>}
                      </div>
                    </td>
                    <td className="px-3 text-xs text-zinc-400">
                      {ASSET_LABEL[q.asset_class] ?? q.asset_class}
                    </td>
                    <td className="tabular px-3 text-right text-zinc-200">{num(q.price)}</td>
                    <td className={`tabular px-3 text-right ${chg.cls}`}>{chg.text}</td>
                    <td className={`tabular px-3 text-right ${rsiCls(q.rsi)}`}>{num(q.rsi, 0)}</td>
                    <td className="tabular px-3 text-right text-zinc-300">{num(q.macd)}</td>
                    <td className="tabular px-3 text-right text-zinc-300">{num(q.atr)}</td>
                    <td className="pl-3 text-center">
                      {(() => {
                        const title = freshnessTitle(q.freshness, q.bar_time, q.asset_class);
                        const marketClosed = title.startsWith("Market closed");
                        // When the market is closed use neutral blue instead of amber: not "bad data" but
                        // "exchange closed".
                        const dot = marketClosed ? "bg-sky-500" : freshnessDot(q.freshness);
                        return (
                          <span
                            className={`inline-block h-2.5 w-2.5 rounded-full ${dot}`}
                            title={title}
                          />
                        );
                      })()}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
