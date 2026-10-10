// localStorage persistence for the cockpit state — the layout is stored in the browser.
// A corrupt/outdated schema falls back to the default (silently, no crash).

import type { DashboardState, DashboardTab } from "./types";

const KEY = "argus.dashboard.v1";

/** Unique widget/tab id (from crypto when available, otherwise random). */
export function uid(prefix = "w"): string {
  const rnd =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(36).slice(2, 10);
  return `${prefix}-${rnd}`;
}

/** The sample "Watch Panel" shown on first launch — populated with Argus's main widgets. */
function defaultTab(): DashboardTab {
  const widgets = [
    { i: "crypto", type: "crypto" as const },
    { i: "watchlist", type: "watchlist" as const },
    { i: "quote", type: "quote" as const },
    { i: "chart", type: "chart" as const },
    { i: "portfolio", type: "portfolio" as const },
    { i: "news", type: "news" as const },
    { i: "alerts", type: "alerts" as const },
    { i: "journal", type: "journal" as const },
    { i: "hitrate", type: "hitrate" as const },
    { i: "health", type: "health" as const },
  ];
  const layout = [
    { i: "crypto", x: 0, y: 0, w: 8, h: 14, minW: 5, minH: 9 },
    { i: "watchlist", x: 8, y: 0, w: 4, h: 8, minW: 4, minH: 4 },
    { i: "quote", x: 8, y: 8, w: 4, h: 6, minW: 3, minH: 5 },
    { i: "chart", x: 0, y: 14, w: 8, h: 12, minW: 4, minH: 7 },
    { i: "portfolio", x: 8, y: 14, w: 4, h: 12, minW: 3, minH: 5 },
    { i: "news", x: 8, y: 26, w: 4, h: 12, minW: 3, minH: 5 },
    { i: "alerts", x: 0, y: 26, w: 8, h: 8, minW: 4, minH: 4 },
    { i: "journal", x: 0, y: 34, w: 8, h: 9, minW: 4, minH: 4 },
    { i: "hitrate", x: 8, y: 38, w: 4, h: 6, minW: 3, minH: 5 },
    { i: "health", x: 0, y: 43, w: 8, h: 6, minW: 3, minH: 4 },
  ];
  return { id: "panel", name: "Watch Panel", widgets, layout };
}

export function defaultState(): DashboardState {
  const tab = defaultTab();
  return { version: 1, tabs: [tab], activeTabId: tab.id };
}

export function loadState(): DashboardState {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return defaultState();
    const parsed = JSON.parse(raw) as DashboardState;
    if (parsed?.version !== 1 || !Array.isArray(parsed.tabs) || parsed.tabs.length === 0) {
      return defaultState();
    }
    return parsed;
  } catch {
    return defaultState();
  }
}

export function saveState(state: DashboardState): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    // quota full / private mode — keep working without persistence
  }
}
