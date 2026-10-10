import { useEffect, useState } from "react";
import { api, type PortfolioResponse, type PositionItem } from "../api";
import { PENDING } from "../format";

function money(v: number | null, ccy: "₺" | "$"): string {
  if (v == null) return "—";
  return `${ccy}${v.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

function pnlCls(v: number | null): string {
  if (v == null) return "text-zinc-500";
  return v > 0 ? "text-emerald-400" : v < 0 ? "text-red-400" : "text-zinc-400";
}

function PositionRow({ p }: { p: PositionItem }) {
  const arrow = (p.pnl_pct ?? 0) > 0 ? "▲" : (p.pnl_pct ?? 0) < 0 ? "▼" : "·";
  return (
    <div className="rounded-lg border border-zinc-800 p-2.5">
      <div className="flex items-center justify-between">
        <span className="font-medium text-zinc-200">{p.symbol}</span>
        {p.owner && <span className="text-[10px] text-zinc-500">{p.owner}</span>}
      </div>
      <div className="tabular mt-1 flex items-center justify-between text-xs text-zinc-400">
        <span>
          {p.quantity} × {p.avg_cost.toLocaleString("en-US")} {p.cost_currency}
        </span>
        {p.pnl_pct != null && (
          <span className={pnlCls(p.pnl_pct)}>
            {arrow} {p.pnl_pct >= 0 ? "+" : ""}
            {p.pnl_pct.toFixed(1)}%
          </span>
        )}
      </div>
      <div className="tabular mt-1.5 flex items-center justify-between text-xs">
        <span className="text-zinc-500">Value</span>
        <span className="text-zinc-300">
          {money(p.value_try, "₺")} · {money(p.value_usd, "$")}
        </span>
      </div>
      <div className="tabular flex items-center justify-between text-xs">
        <span className="text-zinc-500">P/L</span>
        <span className={pnlCls(p.pnl_usd)}>
          {money(p.pnl_try, "₺")} · {money(p.pnl_usd, "$")}
        </span>
      </div>
    </div>
  );
}

/** Portfolio card. Shows the holdings defined in config. */
export function PortfolioPanel() {
  const [pf, setPf] = useState<PortfolioResponse | null>(null);

  useEffect(() => {
    let active = true;
    const load = () =>
      api
        .portfolio()
        .then((d) => active && setPf(d))
        .catch(() => active && setPf(null));
    void load();
    const t = setInterval(load, 15_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  return (
    <div className="p-3">
      {pf === null ? (
        <p className="text-sm text-zinc-500">Loading…</p>
      ) : !pf.connected ? (
        <p className="text-sm text-zinc-500">
          Portfolio module not connected yet — {PENDING}
        </p>
      ) : (
        <div className="space-y-3">
          <div className="rounded-lg bg-zinc-800/50 p-2.5 text-xs">
            <div className="tabular flex items-center justify-between">
              <span className="text-zinc-500">Total value</span>
              <span className="text-zinc-200">
                {money(pf.total_value_try, "₺")} · {money(pf.total_value_usd, "$")}
              </span>
            </div>
            <div className="tabular mt-1 flex items-center justify-between">
              <span className="text-zinc-500">Total P/L</span>
              <span className={pnlCls(pf.total_pnl_usd)}>
                {money(pf.total_pnl_try, "₺")} · {money(pf.total_pnl_usd, "$")}
              </span>
            </div>
          </div>
          {pf.positions.map((p) => (
            <PositionRow key={p.symbol} p={p} />
          ))}
        </div>
      )}
    </div>
  );
}
