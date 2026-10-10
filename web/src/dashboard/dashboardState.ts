import { createContext, useContext } from "react";

export interface DashboardCtx {
  activeSymbol: string | null;
  setActiveSymbol: (s: string) => void;
  symbols: string[];
  /** Symbol → timeframes defined in config. */
  timeframesBySymbol: Record<string, string[]>;
}

export const DashboardContext = createContext<DashboardCtx | null>(null);

export function useDashboard(): DashboardCtx {
  const ctx = useContext(DashboardContext);
  if (!ctx) throw new Error("useDashboard must be used inside DashboardProvider");
  return ctx;
}
