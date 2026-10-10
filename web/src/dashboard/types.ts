// Argus cockpit — dashboard data model (movable widget grid).
// Each dashboard is a tab; each tab holds widget instances + a react-grid-layout layout.

import type { Layout } from "react-grid-layout";

/** Placeable widget types — all bound to Argus's REAL data (no empty boxes). */
export type WidgetType =
  | "watchlist"
  | "chart"
  | "quote"
  | "crypto"
  | "portfolio"
  | "hitrate"
  | "alerts"
  | "journal"
  | "news"
  | "proposal"
  | "cost"
  | "health";

/** A single widget instance on a dashboard. `i` is both the React key and the RGL layout key. */
export interface WidgetInstance {
  i: string;
  type: WidgetType;
  /** Pinned symbol for chart/quote (used while linking is off). */
  symbol?: string;
}

/** A dashboard tab: its name, widgets and layout. */
export interface DashboardTab {
  id: string;
  name: string;
  widgets: WidgetInstance[];
  layout: Layout[];
}

/** The whole cockpit state — stored in localStorage (v1 schema). */
export interface DashboardState {
  version: 1;
  tabs: DashboardTab[];
  activeTabId: string;
}
