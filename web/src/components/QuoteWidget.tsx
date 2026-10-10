import { useEffect, useState } from "react";
import { api, type QuoteItem } from "../api";
import { PENDING, changeCell, freshnessTitle, istanbul, num, rsiCls } from "../format";
import { useDashboard } from "../dashboard/dashboardState";
import { SymbolPicker } from "./SymbolPicker";
import { WidgetFrame } from "../dashboard/WidgetFrame";
import type { WidgetInstance } from "../dashboard/types";

const ASSET_LABEL: Record<string, string> = {
  bist: "BIST",
  metal: "Precious Metal",
  crypto: "Crypto",
  fx: "FX",
  equity: "Equity",
};

const FRESH_LABEL: Record<string, { text: string; cls: string }> = {
  fresh: { text: "● fresh", cls: "text-emerald-400" },
  stale: { text: "● stale", cls: "text-amber-400" },
  none: { text: "● no data", cls: "text-red-400" },
};

function Stat({
  label,
  value,
  cls,
  title,
}: {
  label: string;
  value: string;
  cls?: string;
  title?: string;
}) {
  return (
    <div className="flex items-center justify-between border-b border-zinc-800/60 py-1.5 text-sm">
      <span className="text-zinc-500">{label}</span>
      <span className={`tabular ${cls ?? "text-zinc-200"}`} title={title}>
        {value}
      </span>
    </div>
  );
}

/** Single-symbol statistics widget (like OpenBB "Share Statistics") — price + indicators.
 *  Data from /api/quotes; a symbol is picked or it follows the active symbol (link). */
export function QuoteWidget({
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
  const [quotes, setQuotes] = useState<QuoteItem[] | null>(null);

  const symbol = linked ? activeSymbol : (instance.symbol ?? activeSymbol);

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

  function pickSymbol(s: string) {
    if (linked) setActiveSymbol(s);
    else update({ symbol: s });
  }

  const q = quotes?.find((x) => x.symbol === symbol) ?? null;
  const chg = changeCell(q?.change_pct_24h ?? null);
  const freshHint = q ? freshnessTitle(q.freshness, q.bar_time, q.asset_class) : "";
  const marketClosed = freshHint.startsWith("Market closed");
  // When the market is closed show "market closed" (neutral blue) instead of "stale": the data
  // is not bad, the exchange is closed.
  const fresh = q
    ? marketClosed
      ? { text: "● market closed", cls: "text-sky-400" }
      : FRESH_LABEL[q.freshness]
    : null;

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

  return (
    <WidgetFrame
      title={symbol ?? "Statistics"}
      subtitle={q ? (ASSET_LABEL[q.asset_class] ?? q.asset_class) : undefined}
      icon="📊"
      onClose={onClose}
      headerRight={header}
    >
      <div className="p-3">
        {quotes === null ? (
          <p className="text-sm text-zinc-500">Loading…</p>
        ) : !symbol ? (
          <p className="text-sm text-zinc-500">{PENDING} — no symbol selected</p>
        ) : !q ? (
          <p className="text-sm text-zinc-500">{PENDING} — no data for {symbol}</p>
        ) : (
          <>
            <div className="mb-3 flex items-baseline justify-between">
              <span className="tabular text-2xl font-semibold text-zinc-100">
                {num(q.price)}
              </span>
              <span className={`tabular text-sm font-medium ${chg.cls}`}>{chg.text}</span>
            </div>
            <Stat label="RSI (14)" value={num(q.rsi, 0)} cls={rsiCls(q.rsi)} />
            <Stat label="MACD" value={num(q.macd)} />
            <Stat label="MACD signal" value={num(q.macd_signal)} />
            <Stat label="ATR" value={num(q.atr)} />
            <Stat label="Candle count" value={String(q.bar_count)} />
            <Stat
              label="Freshness"
              value={fresh?.text ?? "—"}
              cls={fresh?.cls}
              title={freshHint}
            />
            {q.bar_time && (
              <p className="mt-1.5 text-[10px] text-zinc-500">
                Last candle: {istanbul(q.bar_time)}
                {marketClosed && " · exchange closed on weekends (this is the latest data)"}
              </p>
            )}
            <p className="mt-3 text-[10px] leading-snug text-zinc-500">
              Evidence tier [A] computed indicators · Not financial advice.
            </p>
          </>
        )}
      </div>
    </WidgetFrame>
  );
}
