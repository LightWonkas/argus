// Widget registry — type → label, icon, default size and render function.
// Feeds the "add" menu and the grid. Every widget is bound to Argus's REAL data.

import { AlertFeed } from "../components/AlertFeed";
import { AnalysisJournal } from "../components/AnalysisJournal";
import { ChartWidget } from "../components/ChartWidget";
import { CostPanel } from "../components/CostPanel";
import { CryptoOverview } from "../components/CryptoOverview";
import { HealthPanel } from "../components/HealthPanel";
import { HitRatePanel } from "../components/HitRatePanel";
import { NewsFeed } from "../components/NewsFeed";
import { PortfolioPanel } from "../components/PortfolioPanel";
import { ProposalWidget } from "../components/ProposalWidget";
import { QuoteWidget } from "../components/QuoteWidget";
import { WatchWidget } from "./WatchWidget";
import { WidgetFrame } from "./WidgetFrame";
import type { WidgetInstance, WidgetType } from "./types";

/** Context passed when rendering a widget. */
export interface WidgetCtx {
  instance: WidgetInstance;
  onClose: () => void;
  update: (patch: Partial<WidgetInstance>) => void;
}

export interface WidgetMeta {
  label: string;
  icon: string;
  /** Grid size when newly added (12 columns). */
  size: { w: number; h: number; minW: number; minH: number };
  render: (ctx: WidgetCtx) => React.ReactNode;
}

export const WIDGETS: Record<WidgetType, WidgetMeta> = {
  watchlist: {
    label: "Watchlist / Holdings",
    icon: "👁",
    size: { w: 8, h: 8, minW: 4, minH: 4 },
    render: ({ onClose }) => <WatchWidget onClose={onClose} />,
  },
  chart: {
    label: "Price Chart",
    icon: "📈",
    size: { w: 8, h: 12, minW: 4, minH: 7 },
    render: (ctx) => (
      <ChartWidget instance={ctx.instance} onClose={ctx.onClose} update={ctx.update} />
    ),
  },
  quote: {
    label: "Statistics (single symbol)",
    icon: "📊",
    size: { w: 4, h: 8, minW: 3, minH: 5 },
    render: (ctx) => (
      <QuoteWidget instance={ctx.instance} onClose={ctx.onClose} update={ctx.update} />
    ),
  },
  crypto: {
    label: "Crypto Monitor",
    icon: "₿",
    size: { w: 8, h: 14, minW: 5, minH: 9 },
    render: (ctx) => (
      <CryptoOverview instance={ctx.instance} onClose={ctx.onClose} update={ctx.update} />
    ),
  },
  portfolio: {
    label: "Portfolio",
    icon: "💼",
    size: { w: 4, h: 12, minW: 3, minH: 5 },
    render: ({ onClose }) => (
      <WidgetFrame title="Portfolio" icon="💼" onClose={onClose}>
        <PortfolioPanel />
      </WidgetFrame>
    ),
  },
  hitrate: {
    label: "Hit Rate / Self-Monitoring",
    icon: "🎯",
    size: { w: 4, h: 6, minW: 3, minH: 5 },
    render: ({ onClose }) => (
      <WidgetFrame title="Hit Rate / Self-Monitoring" icon="🎯" onClose={onClose}>
        <HitRatePanel />
      </WidgetFrame>
    ),
  },
  alerts: {
    label: "Live Alert Feed",
    icon: "🔔",
    size: { w: 8, h: 8, minW: 4, minH: 4 },
    render: ({ onClose }) => (
      <WidgetFrame title="Live Alert Feed" icon="🔔" onClose={onClose}>
        <AlertFeed />
      </WidgetFrame>
    ),
  },
  journal: {
    label: "Analysis Journal",
    icon: "📒",
    size: { w: 8, h: 9, minW: 4, minH: 4 },
    render: ({ onClose }) => (
      <WidgetFrame title="Analysis Journal" icon="📒" onClose={onClose}>
        <AnalysisJournal />
      </WidgetFrame>
    ),
  },
  news: {
    label: "News / KAP",
    icon: "📰",
    size: { w: 4, h: 12, minW: 3, minH: 5 },
    render: ({ onClose }) => (
      <WidgetFrame title="News / KAP" icon="📰" onClose={onClose}>
        <NewsFeed />
      </WidgetFrame>
    ),
  },
  proposal: {
    label: "Confluence Proposal",
    icon: "🧩",
    size: { w: 4, h: 9, minW: 3, minH: 5 },
    render: (ctx) => (
      <ProposalWidget instance={ctx.instance} onClose={ctx.onClose} update={ctx.update} />
    ),
  },
  cost: {
    label: "Cost (LLM budget)",
    icon: "💸",
    size: { w: 4, h: 8, minW: 3, minH: 5 },
    render: ({ onClose }) => (
      <WidgetFrame title="Cost (LLM budget)" icon="💸" onClose={onClose}>
        <CostPanel />
      </WidgetFrame>
    ),
  },
  health: {
    label: "Component Health",
    icon: "🩺",
    size: { w: 8, h: 6, minW: 3, minH: 4 },
    render: ({ onClose }) => (
      <WidgetFrame title="Component Health" icon="🩺" onClose={onClose}>
        <HealthPanel />
      </WidgetFrame>
    ),
  },
};

/** Order of the "add" menu. */
export const WIDGET_ORDER: WidgetType[] = [
  "crypto",
  "chart",
  "quote",
  "watchlist",
  "portfolio",
  "news",
  "proposal",
  "alerts",
  "journal",
  "hitrate",
  "cost",
  "health",
];
