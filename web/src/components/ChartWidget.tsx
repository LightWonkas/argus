import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createChart,
  type IChartApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { ApiError, api, type MeasurementView, type ProposalResponse } from "../api";
import { PENDING } from "../format";
import { DataState } from "./DataState";
import { ema, rsi } from "../indicators";
import { useDashboard } from "../dashboard/dashboardState";
import { SymbolPicker } from "./SymbolPicker";
import { WidgetFrame } from "../dashboard/WidgetFrame";
import type { WidgetInstance } from "../dashboard/types";

/** Indicator on/off toggle button in the header bar. */
function Toggle({
  on,
  onClick,
  children,
  color,
}: {
  on: boolean;
  onClick: () => void;
  children: React.ReactNode;
  color?: string;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={on}
      className={`rounded px-1.5 py-0.5 text-left text-[10px] font-medium transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 ${
        on ? "bg-zinc-700 text-zinc-100" : "text-zinc-500 hover:text-zinc-300"
      }`}
      style={on && color ? { color } : undefined}
    >
      {children}
    </button>
  );
}

/** Advanced price chart widget — candles + volume + EMA(20/50) overlay + RSI(14) sub-pane.
 *  Symbol picker + "link" (follow the active symbol). OHLCV from the store; indicators are
 *  computed on the client. */
export function ChartWidget({
  instance,
  onClose,
  update,
}: {
  instance: WidgetInstance;
  onClose: () => void;
  update: (patch: Partial<WidgetInstance>) => void;
}) {
  const { activeSymbol, setActiveSymbol, symbols, timeframesBySymbol } = useDashboard();
  const [linked, setLinked] = useState(instance.symbol == null);
  const [showEmaFast, setShowEmaFast] = useState(true);
  const [showEmaSlow, setShowEmaSlow] = useState(true);
  const [showRsi, setShowRsi] = useState(true);
  const [showVol, setShowVol] = useState(true);
  // Indicator toggles live in the ⚙ popover, not the header (the header stays clean).
  const [gearOpen, setGearOpen] = useState(false);
  // Measurement threshold lines (📐): the ref ± k×ATR the hit-rate evaluation tests — NOT a target.
  const [showPlan, setShowPlan] = useState(true);
  const [proposal, setProposal] = useState<ProposalResponse | null>(null);
  // Timeframe selection — only TFs defined in config are offered (no fake options).
  const [tf, setTf] = useState<string | null>(null);

  // Effective symbol: follows the active symbol when linked, otherwise its own pinned symbol.
  const symbol = linked ? activeSymbol : (instance.symbol ?? activeSymbol);
  const availableTfs = symbol ? (timeframesBySymbol[symbol] ?? []) : [];
  // If the selected TF is not in the symbol's list (symbol changed), fall back to the first TF.
  const effectiveTf =
    tf && availableTfs.includes(tf) ? tf : (availableTfs[0] ?? null);

  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const legendRef = useRef<HTMLDivElement>(null); // crosshair OHLC box (written without a render)
  const [err, setErr] = useState<string | null>(null);
  const [meta, setMeta] = useState<{ tf: string; count: number } | null>(null);

  // Current confluence proposal for the active symbol — for threshold lines (none → not drawn).
  useEffect(() => {
    setProposal(null);
    if (!symbol) return;
    let active = true;
    const load = () =>
      api
        .proposal(symbol)
        .then((p) => active && setProposal(p))
        .catch(() => active && setProposal(null));
    void load();
    const t = setInterval(load, 60_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [symbol]);

  function pickSymbol(s: string) {
    if (linked) setActiveSymbol(s);
    else update({ symbol: s });
  }

  useEffect(() => {
    const el = containerRef.current;
    if (!el || !symbol) return;
    setErr(null);

    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { color: "transparent" },
        textColor: "#a1a1aa",
        fontFamily: "ui-monospace, monospace",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "#27272a" },
        horzLines: { color: "#27272a" },
      },
      timeScale: { borderColor: "#3f3f46", timeVisible: true },
      rightPriceScale: { borderColor: "#3f3f46" },
      crosshair: { mode: 0 },
    });
    chartRef.current = chart;

    const candle = chart.addSeries(CandlestickSeries, {
      upColor: "#10b981",
      downColor: "#ef4444",
      wickUpColor: "#10b981",
      wickDownColor: "#ef4444",
      borderVisible: false,
    });

    let active = true;
    api
      .ohlcv(symbol, effectiveTf ?? undefined)
      .then((d) => {
        if (!active) return;
        const candles = d.candles;
        candle.setData(
          candles.map((c) => ({
            time: c.time as UTCTimestamp,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
          })),
        );

        if (showVol) {
          const vol = chart.addSeries(HistogramSeries, {
            priceFormat: { type: "volume" },
            priceScaleId: "vol",
          });
          chart.priceScale("vol").applyOptions({
            scaleMargins: { top: 0.82, bottom: 0 },
          });
          vol.setData(
            candles.map((c) => ({
              time: c.time as UTCTimestamp,
              value: c.volume,
              color: c.close >= c.open ? "#10b98155" : "#ef444455",
            })),
          );
        }

        const closes = candles.map((c) => c.close);
        const overlay = (
          values: (number | null)[],
        ): { time: UTCTimestamp; value: number }[] =>
          values
            .map((v, i) =>
              v == null ? null : { time: candles[i].time as UTCTimestamp, value: v },
            )
            .filter((p): p is { time: UTCTimestamp; value: number } => p !== null);

        if (showEmaFast) {
          const s = chart.addSeries(LineSeries, {
            color: "#38bdf8",
            lineWidth: 1,
            priceLineVisible: false,
            lastValueVisible: false,
          });
          s.setData(overlay(ema(closes, 20)));
        }
        if (showEmaSlow) {
          const s = chart.addSeries(LineSeries, {
            color: "#f59e0b",
            lineWidth: 1,
            priceLineVisible: false,
            lastValueVisible: false,
          });
          s.setData(overlay(ema(closes, 50)));
        }

        if (showRsi) {
          const rsiSeries = chart.addSeries(
            LineSeries,
            { color: "#a78bfa", lineWidth: 1, priceLineVisible: false },
            1, // pane 1 (sub-pane)
          );
          rsiSeries.setData(overlay(rsi(closes, 14)));
          rsiSeries.createPriceLine({
            price: 70,
            color: "#ef444466",
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: false,
            title: "",
          });
          rsiSeries.createPriceLine({
            price: 30,
            color: "#10b98166",
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: false,
            title: "",
          });
          try {
            chart.panes()[1]?.setHeight(90);
          } catch {
            /* older version — pane height stays at its default */
          }
        }

        // 📐 Measurement thresholds: the ref ± k×ATR the hit-rate evaluation tests this proposal
        // against. NOT a target/stop — the labels say so too. No proposal/measurement → no lines.
        const m: MeasurementView | null | undefined = proposal?.measurement;
        if (showPlan && m && proposal && proposal.symbol === symbol) {
          const dir = proposal.direction;
          const upColor =
            dir === "bullish" ? "#34d399" : dir === "bearish" ? "#fb7185" : "#71717a";
          const downColor =
            dir === "bearish" ? "#34d399" : dir === "bullish" ? "#fb7185" : "#71717a";
          const mark = (c: string) => (c === "#34d399" ? "hit" : c === "#fb7185" ? "miss" : "threshold");
          candle.createPriceLine({
            price: m.ref_price,
            color: "#71717a",
            lineWidth: 1,
            lineStyle: 3,
            axisLabelVisible: true,
            title: "ref",
          });
          candle.createPriceLine({
            price: m.ref_price + m.threshold_abs,
            color: upColor,
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
            title: `${mark(upColor)} +${m.k_atr}×ATR`,
          });
          candle.createPriceLine({
            price: m.ref_price - m.threshold_abs,
            color: downColor,
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
            title: `${mark(downColor)} −${m.k_atr}×ATR`,
          });
        }

        // Crosshair OHLC box: follows the hovered candle; written to a ref, not state (perf).
        const fmt = (v: number) =>
          v.toLocaleString("en-US", { maximumFractionDigits: 2 });
        chart.subscribeCrosshairMove((param) => {
          const el = legendRef.current;
          if (!el) return;
          const bar = param.seriesData.get(candle) as
            | { open: number; high: number; low: number; close: number }
            | undefined;
          if (!bar || param.time == null) {
            el.style.display = "none";
            return;
          }
          const i = candles.findIndex((c) => c.time === param.time);
          const vol = i >= 0 ? candles[i].volume : null;
          const date = new Date((param.time as number) * 1000).toLocaleDateString("en-US", {
            day: "2-digit",
            month: "short",
            year: "numeric",
          });
          const chg = ((bar.close - bar.open) / bar.open) * 100;
          el.style.display = "block";
          el.innerHTML =
            `<span class="text-zinc-500">${date}</span> ` +
            `O <span class="text-zinc-200">${fmt(bar.open)}</span> ` +
            `H <span class="text-zinc-200">${fmt(bar.high)}</span> ` +
            `L <span class="text-zinc-200">${fmt(bar.low)}</span> ` +
            `C <span class="${chg >= 0 ? "text-emerald-400" : "text-rose-400"}">${fmt(bar.close)} ${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</span>` +
            (vol != null
              ? ` <span class="text-zinc-500">vol ${vol.toLocaleString("en-US", { notation: "compact", maximumFractionDigits: 1 })}</span>`
              : "");
        });

        chart.timeScale().fitContent();
        setMeta({ tf: d.timeframe, count: candles.length });
      })
      .catch((e) => {
        if (!active) return;
        setErr(
          e instanceof ApiError && e.status === 404
            ? `${PENDING} — no stored candles for ${symbol} (fills once the watch loop runs)`
            : "Could not load the chart",
        );
      });

    return () => {
      active = false;
      chart.remove();
      chartRef.current = null;
    };
  }, [symbol, effectiveTf, showEmaFast, showEmaSlow, showRsi, showVol, showPlan, proposal]);

  const subtitle = useMemo(
    () => (meta ? `${meta.tf} · ${meta.count} candles` : undefined),
    [meta],
  );

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
      {availableTfs.length > 1 && (
        <>
          <span className="mx-0.5 h-3 w-px bg-zinc-700" />
          {availableTfs.map((t) => (
            <Toggle key={t} on={t === effectiveTf} onClick={() => setTf(t)}>
              {t}
            </Toggle>
          ))}
        </>
      )}
      {/* 📐 measurement threshold lines — only meaningful with a proposal + ATR */}
      {proposal?.measurement && (
        <button
          onClick={() => setShowPlan((v) => !v)}
          aria-pressed={showPlan}
          title={`Measurement thresholds: ref ± ${proposal.measurement.k_atr}×ATR (hit-rate evaluation — not a target)`}
          className={`rounded px-1.5 py-0.5 text-xs focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 ${
            showPlan ? "bg-violet-500/20 text-violet-300" : "text-zinc-500 hover:text-zinc-300"
          }`}
        >
          📐
        </button>
      )}
      {/* Indicator settings live in a popover — clean header (symbol · link · TF · ⚙). */}
      <div className="relative">
        <button
          onClick={() => setGearOpen((v) => !v)}
          title="Indicators"
          aria-expanded={gearOpen}
          className={`rounded px-1.5 py-0.5 text-xs focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 ${
            gearOpen ? "bg-zinc-700 text-zinc-100" : "text-zinc-500 hover:text-zinc-300"
          }`}
        >
          ⚙
        </button>
        {gearOpen && (
          <div className="absolute right-0 top-7 z-20 flex flex-col gap-1 rounded-lg border border-zinc-700 bg-zinc-900 p-2 shadow-xl shadow-black/50">
            <span className="px-1 text-[10px] uppercase tracking-wide text-zinc-500">
              Indicators
            </span>
            <Toggle on={showEmaFast} onClick={() => setShowEmaFast((v) => !v)} color="#38bdf8">
              EMA20
            </Toggle>
            <Toggle on={showEmaSlow} onClick={() => setShowEmaSlow((v) => !v)} color="#f59e0b">
              EMA50
            </Toggle>
            <Toggle on={showRsi} onClick={() => setShowRsi((v) => !v)} color="#a78bfa">
              RSI(14)
            </Toggle>
            <Toggle on={showVol} onClick={() => setShowVol((v) => !v)}>
              Volume
            </Toggle>
          </div>
        )}
      </div>
    </div>
  );

  return (
    <WidgetFrame
      title={symbol ? `${symbol}` : "Chart"}
      subtitle={subtitle}
      icon="📈"
      onClose={onClose}
      headerRight={header}
    >
      {!symbol ? (
        <DataState reason="no symbol selected — click a row in the watchlist" />
      ) : err ? (
        <DataState
          reason={`no stored candles for ${symbol}`}
          hint="python scripts/feed_once.py"
        />
      ) : (
        <div className="relative h-full w-full">
          <div ref={containerRef} className="h-full w-full" />
          {/* Crosshair OHLC box — subscribeCrosshairMove writes to it directly */}
          <div
            ref={legendRef}
            style={{ display: "none" }}
            className="tabular pointer-events-none absolute left-2 top-2 z-10 rounded-md border border-zinc-700/80 bg-zinc-900/90 px-2.5 py-1 font-mono text-[11px] text-zinc-400"
          />
        </div>
      )}
    </WidgetFrame>
  );
}
