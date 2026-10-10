import { useEffect, useMemo, useState } from "react";
import {
  api,
  type CryptoDashboardResponse,
  type CryptoShadowPosition,
} from "../api";
import { DataState } from "./DataState";
import { SymbolPicker } from "./SymbolPicker";
import { useDashboard } from "../dashboard/dashboardState";
import { WidgetFrame } from "../dashboard/WidgetFrame";
import type { WidgetInstance } from "../dashboard/types";

type LoadState = "loading" | "ok" | "error";
type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function asNumber(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function numberText(value: unknown, digits = 2): string {
  const parsed = asNumber(value);
  return parsed == null ? "—" : parsed.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function pctFraction(value: unknown, digits = 4): string {
  const parsed = asNumber(value);
  return parsed == null ? "—" : `${(parsed * 100).toFixed(digits)}%`;
}

function basisPct(data: CryptoDashboardResponse): number | null {
  const spot = asNumber(data.basis?.spot_price);
  const perpetual = asNumber(data.basis?.perpetual_price);
  return spot && perpetual != null ? ((perpetual - spot) / spot) * 100 : null;
}

function sumPnL(positions: CryptoShadowPosition[], key: "realized_pnl" | "unrealized_pnl") {
  if (positions.length === 0) return null;
  return positions.reduce((sum, position) => sum + (asNumber(position[key]) ?? 0), 0);
}

function Stat({ label, value, tone = "text-zinc-200" }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-zinc-800/60 py-1.5 text-xs">
      <span className="text-zinc-500">{label}</span>
      <span className={`tabular text-right ${tone}`}>{value}</span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-md border border-zinc-800 bg-zinc-900/35 p-2.5">
      <h3 className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
        {title}
      </h3>
      {children}
    </section>
  );
}

function statusTone(status: CryptoDashboardResponse["data_status"]): string {
  if (status === "ok") return "text-emerald-400";
  if (status === "pending") return "text-amber-400";
  return "text-rose-400";
}

function statusLabel(status: CryptoDashboardResponse["data_status"]): string {
  return {
    ok: "ACCEPTED",
    pending: "DATA MISSING",
    quarantined: "QUARANTINED",
    rejected: "REJECTED",
  }[status];
}

/** Shows spot/perpetual, GUARD and shadow results from the same persistent PIT source. */
export function CryptoOverview({
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
  const [data, setData] = useState<CryptoDashboardResponse | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const symbol = linked ? activeSymbol : (instance.symbol ?? activeSymbol);

  useEffect(() => {
    if (!symbol) {
      setData(null);
      setLoadState("ok");
      return;
    }
    let active = true;
    setLoadState("loading");
    const load = () => {
      void api
        .cryptoDashboard(symbol)
        .then((response) => {
          if (!active) return;
          setData(response);
          setLoadState("ok");
        })
        .catch(() => {
          if (!active) return;
          setLoadState("error");
        });
    };
    load();
    const timer = setInterval(load, 15_000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [symbol]);

  const featureValues = useMemo(() => {
    const root = record(data?.features?.payload);
    return record(root?.values) ?? root ?? {};
  }, [data]);
  const qualityDecision = record(data?.quality?.payload)?.decision;
  const regime = record(data?.regime?.payload)?.regime;
  const expertPayload = data?.experts?.payload;
  const experts = Array.isArray(expertPayload)
    ? expertPayload.map(record).filter((item): item is UnknownRecord => item !== null)
    : [];
  const backtest = record(data?.backtest?.payload);
  const metrics = record(backtest?.metrics);

  function pickSymbol(next: string) {
    if (linked) setActiveSymbol(next);
    else update({ symbol: next });
  }

  const header = (
    <div className="flex items-center gap-1">
      <SymbolPicker value={symbol} symbols={symbols} onChange={pickSymbol} />
      <button
        onClick={() => setLinked((value) => !value)}
        title={linked ? "Following the active symbol" : "Symbol pinned"}
        className={`rounded px-1 text-xs ${linked ? "text-sky-400" : "text-zinc-500"}`}
      >
        {linked ? "🔗" : "🔓"}
      </button>
    </div>
  );

  return (
    <WidgetFrame
      title="Crypto Monitor"
      subtitle={symbol ?? undefined}
      icon="₿"
      onClose={onClose}
      headerRight={header}
    >
      <div className="h-full overflow-y-auto p-3">
        {loadState === "loading" ? (
          <p className="text-sm text-zinc-500">Loading…</p>
        ) : loadState === "error" ? (
          <DataState kind="blind" reason="Could not reach the crypto API; the old value is not treated as current." />
        ) : !symbol ? (
          <DataState reason="No crypto symbol selected." />
        ) : !data || (!data.spot && data.as_of === null) ? (
          <DataState reason={`No stored point-in-time market data for ${symbol}.`} />
        ) : (
          <div className="space-y-2.5">
            <div className="flex items-center justify-between text-[11px]">
              <span className={`font-semibold ${statusTone(data.data_status)}`}>
                {statusLabel(data.data_status)}
              </span>
              <span className="text-zinc-600">persistent point-in-time record</span>
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              <Section title="Spot / Perpetual">
                <Stat label="Spot last" value={numberText(data.spot?.last_price)} />
                <Stat label="Perp mark" value={numberText(data.perpetual?.mark_price)} />
                <Stat label="Basis" value={numberText(basisPct(data), 4)} />
                <Stat label="Spread (bps)" value={numberText(featureValues.spread_bps, 3)} />
              </Section>

              <Section title="Derivatives / Order flow">
                <Stat label="Funding" value={pctFraction(data.funding?.rate)} />
                <Stat label="Open interest" value={numberText(data.open_interest?.contracts, 4)} />
                <Stat label="Long/short" value={numberText(data.long_short_ratio?.account_ratio, 4)} />
                <Stat label="Book imbalance" value={numberText(featureValues.book_imbalance_10, 4)} />
                <Stat label="Liquidation events" value={String(data.liquidations.length)} />
              </Section>

              <Section title="Regime / Decision">
                <Stat
                  label="Data Quality Gate"
                  value={typeof qualityDecision === "string" ? qualityDecision.toUpperCase() : "—"}
                />
                <Stat label="Regime" value={typeof regime === "string" ? regime : "—"} />
                <Stat
                  label="TradeCandidate"
                  value={
                    data.trade_candidate
                      ? `${data.trade_candidate.side.toUpperCase()} · ${data.trade_candidate.confidence_pct}%`
                      : "NO RECORD"
                  }
                />
                <Stat
                  label="GUARD"
                  value={data.guard?.decision?.toUpperCase() ?? "NO RECORD"}
                  tone={data.guard?.decision === "allow_shadow" ? "text-emerald-400" : "text-amber-400"}
                />
                <Stat
                  label="Live orders"
                  value={data.trade_candidate?.execution_enabled === false ? "DISABLED" : "—"}
                  tone="text-rose-400"
                />
              </Section>

              <Section title="Shadow / Backtest">
                <Stat label="Shadow positions" value={String(data.shadow_positions.length)} />
                <Stat
                  label="Realized PnL"
                  value={numberText(sumPnL(data.shadow_positions, "realized_pnl"))}
                />
                <Stat
                  label="Unrealized PnL"
                  value={numberText(sumPnL(data.shadow_positions, "unrealized_pnl"))}
                />
                <Stat label="Backtest net" value={numberText(metrics?.net_return_pct, 3)} />
                <Stat label="Max drawdown" value={numberText(metrics?.max_drawdown_pct, 3)} />
              </Section>
            </div>

            {experts.length > 0 && (
              <Section title="Expert opinions / contradictions">
                <div className="space-y-1 text-xs">
                  {experts.map((expert, index) => {
                    const contradictions = Array.isArray(expert.contradictions)
                      ? expert.contradictions.join(" · ")
                      : "";
                    return (
                      <div key={`${String(expert.role)}-${index}`} className="border-b border-zinc-800/60 py-1">
                        <span className="text-zinc-300">{String(expert.role ?? "expert")}</span>
                        <span className="ml-2 text-zinc-500">{String(expert.stance ?? "no_data")}</span>
                        {contradictions && <p className="text-amber-400">Contradiction: {contradictions}</p>}
                      </div>
                    );
                  })}
                </div>
              </Section>
            )}

            {data.missing_data.length > 0 && (
              <div className="rounded border border-amber-500/20 bg-amber-500/5 p-2 text-[10px] text-amber-300/80">
                Missing/pending: {data.missing_data.join(" · ")}
              </div>
            )}
            <p className="text-[10px] text-zinc-600">
              Decision support only. Not financial advice. No shadow (simulated) position can be
              opened without a GUARD record.
            </p>
          </div>
        )}
      </div>
    </WidgetFrame>
  );
}
