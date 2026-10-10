// Shared dashboard state: the "active symbol" link + the list of known symbols.
// A lightweight take on widget grouping — clicking a watchlist row switches the
// "linked" chart/quote widgets to that symbol. The symbol list comes from /api/quotes.

import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { DashboardContext } from "./dashboardState";

export function DashboardProvider({ children }: { children: React.ReactNode }) {
  const [activeSymbol, setActiveSymbol] = useState<string | null>(null);
  const [symbols, setSymbols] = useState<string[]>([]);
  const [timeframesBySymbol, setTimeframesBySymbol] = useState<Record<string, string[]>>({});

  useEffect(() => {
    let active = true;
    const load = () =>
      api
        .quotes()
        .then((qs) => {
          if (!active) return;
          const syms = qs.map((q) => q.symbol);
          setSymbols(syms);
          setTimeframesBySymbol(
            Object.fromEntries(qs.map((q) => [q.symbol, q.timeframes ?? []])),
          );
          // On first load pin the active symbol to the first asset (the user can change it).
          setActiveSymbol((cur) => cur ?? syms[0] ?? null);
        })
        .catch(() => {
          /* silent — symbols stay empty and the widgets show DATA PENDING */
        });
    void load();
    const t = setInterval(load, 30_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  const value = useMemo(
    () => ({ activeSymbol, setActiveSymbol, symbols, timeframesBySymbol }),
    [activeSymbol, symbols, timeframesBySymbol],
  );
  return <DashboardContext.Provider value={value}>{children}</DashboardContext.Provider>;
}
