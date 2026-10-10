import { useEffect, useState } from "react";
import { ApiError, api, type ProposalResponse } from "../api";
import { PENDING, biasBadge } from "../format";
import { useDashboard } from "../dashboard/dashboardState";
import { FeedbackButtons } from "./FeedbackButtons";
import { SymbolPicker } from "./SymbolPicker";
import { WidgetFrame } from "../dashboard/WidgetFrame";
import type { WidgetInstance } from "../dashboard/types";

// Agent name → short label (for the contributor list).
const AGENT_LABEL: Record<string, string> = {
  technical: "Technical",
  risk_context: "Risk/Volatility",
  news_sentiment: "News",
  macro_intermarket: "Macro",
  positioning: "Positioning",
  portfolio: "Portfolio",
  llm_technical: "LLM Technical",
};

// Contributor stance → color + mark (color-blind friendly: there is a mark too).
const STANCE: Record<string, { mark: string; cls: string }> = {
  bullish: { mark: "▲", cls: "text-emerald-400" },
  bearish: { mark: "▼", cls: "text-red-400" },
  neutral: { mark: "●", cls: "text-zinc-400" },
  no_data: { mark: "—", cls: "text-zinc-500" },
};

/** Confluence proposal widget — a synthesis of agent votes (deterministic, /api/proposal).
 *  Follows the active symbol or a pinned one; with no data it honestly shows DATA PENDING.
 *  Not financial advice. */
export function ProposalWidget({
  instance,
  onClose,
  update,
}: {
  instance: WidgetInstance;
  onClose: () => void;
  update: (patch: Partial<WidgetInstance>) => void;
}) {
  const { activeSymbol, setActiveSymbol, symbols } = useDashboard();
  const [linked, setLinked] = useState(instance.symbol == null);
  const [data, setData] = useState<ProposalResponse | null>(null);
  const [status, setStatus] = useState<"loading" | "ok" | "nodata">("loading");

  const symbol = linked ? activeSymbol : (instance.symbol ?? activeSymbol);

  useEffect(() => {
    if (!symbol) {
      setStatus("nodata");
      return;
    }
    let active = true;
    const load = () =>
      api
        .proposal(symbol)
        .then((p) => {
          if (!active) return;
          setData(p);
          setStatus("ok");
        })
        .catch((e) => {
          if (!active) return;
          // 404 = symbol outside config or no OHLCV → honest DATA PENDING (nothing fabricated).
          setStatus(e instanceof ApiError && e.status === 404 ? "nodata" : "nodata");
        });
    void load();
    const t = setInterval(load, 30_000); // confluence changes slowly → 30 s
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [symbol]);

  function pickSymbol(s: string) {
    if (linked) setActiveSymbol(s);
    else update({ symbol: s });
  }

  const header = (
    <div className="flex items-center gap-1">
      <SymbolPicker value={symbol} symbols={symbols} onChange={pickSymbol} />
      <button
        onClick={() => setLinked((v) => !v)}
        title={linked ? "Following the active symbol" : "Symbol pinned"}
        className={`rounded px-1 text-xs ${linked ? "text-sky-400" : "text-zinc-500 hover:text-zinc-300"}`}
      >
        {linked ? "🔗" : "🔓"}
      </button>
    </div>
  );

  const bias = data ? biasBadge(data.direction === "no_data" ? null : data.direction) : null;

  return (
    <WidgetFrame title="Confluence" subtitle={symbol ?? undefined} icon="🧩" onClose={onClose} headerRight={header}>
      <div className="p-3">
        {status === "loading" ? (
          <p className="text-sm text-zinc-500">Loading…</p>
        ) : status === "nodata" || !data ? (
          <p className="text-sm text-zinc-500">
            {PENDING} — {symbol ? `no proposal for ${symbol} (waiting for candle data)` : "no symbol selected"}
          </p>
        ) : (
          <>
            {/* Direction + strength + agreement header */}
            <div className="mb-3 flex items-baseline justify-between">
              <span className={`text-xl font-semibold ${bias?.cls ?? "text-zinc-400"}`}>
                {bias?.label ?? "—"}
              </span>
              <span className="tabular text-xs text-zinc-500">
                strength {data.strength}% · agreement {data.confluence_score}%
              </span>
            </div>

            <p className="mb-3 text-xs leading-snug text-zinc-400">{data.rationale}</p>

            {/* Contributing agent votes (transparency — which agent said what) */}
            <div className="space-y-1">
              <div className="text-[10px] uppercase tracking-wide text-zinc-500">Agent votes</div>
              {data.contributors.map((c) => {
                const s = STANCE[c.stance] ?? STANCE.no_data;
                return (
                  <div
                    key={c.agent}
                    className="flex items-center justify-between border-b border-zinc-800/40 py-1 text-sm"
                  >
                    <span className="text-zinc-400">{AGENT_LABEL[c.agent] ?? c.agent}</span>
                    <span className={`tabular ${s.cls}`}>
                      {s.mark} {c.stance === "no_data" ? "no data" : `${c.confidence_pct}%`}
                    </span>
                  </div>
                );
              })}
            </div>

            {/* Are this symbol's proposals useful? (user→system feedback) */}
            <div className="mt-3 flex items-center justify-between border-t border-zinc-800/60 pt-2">
              <span className="text-[10px] text-zinc-500">Is this proposal useful?</span>
              {symbol && (
                <FeedbackButtons
                  vote={{ ref_type: "proposal", ref_id: symbol, symbol }}
                />
              )}
            </div>

            <p className="mt-2 text-[10px] leading-snug text-zinc-500">{data.disclaimer}</p>
          </>
        )}
      </div>
    </WidgetFrame>
  );
}
