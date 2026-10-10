import { useEffect, useState } from "react";
import { api, type CostResponse } from "../api";
import { PENDING } from "../format";
import { usageBarColor } from "../theme";

/** Cost card: today's LLM spend vs. the cap, line-item breakdown (tokens + web search),
 *  monthly projection and distance to the monthly limit. Data comes from the persistent cost
 *  ledger (the total across all processes). */
export function CostPanel() {
  const [cost, setCost] = useState<CostResponse | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let active = true;
    const load = () =>
      api
        .cost()
        .then((c) => {
          if (active) {
            setCost(c);
            setErr(false);
          }
        })
        .catch(() => active && setErr(true));
    void load();
    const t = setInterval(load, 60_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  if (err) return <p className="p-4 text-sm text-zinc-500">{PENDING} — no cost ledger</p>;
  if (cost == null) return <p className="p-4 text-sm text-zinc-500">Loading…</p>;

  const pct = cost.budget_usd > 0 ? Math.min(cost.spent_today_usd / cost.budget_usd, 1) : 0;
  const barColor = usageBarColor(pct);
  const overMonthly = cost.month_projection_usd > cost.monthly_limit_usd;
  const days = cost.last_days ?? [];
  const maxDay = Math.max(...days.map(([, v]) => v), 0.0001);

  return (
    <div className="space-y-3 p-4 text-sm">
      <div>
        <div className="mb-1 flex items-baseline justify-between">
          <span className="text-zinc-400">Today</span>
          <span className="tabular text-zinc-200">
            ${cost.spent_today_usd.toFixed(4)}{" "}
            <span className="text-zinc-500">/ ${cost.budget_usd.toFixed(2)}</span>
          </span>
        </div>
        <div className="h-2 overflow-hidden rounded bg-zinc-800">
          <div className={`h-full ${barColor}`} style={{ width: `${pct * 100}%` }} />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="rounded border border-zinc-800 p-2">
          <div className="text-zinc-500">Token</div>
          <div className="tabular text-zinc-200">${cost.tokens_usd.toFixed(4)}</div>
        </div>
        <div className="rounded border border-zinc-800 p-2">
          <div className="text-zinc-500">Web search</div>
          <div className="tabular text-zinc-200">${cost.search_usd.toFixed(4)}</div>
        </div>
      </div>

      {Object.keys(cost.by_role).length > 0 && (
        <div className="space-y-1 text-xs">
          {Object.entries(cost.by_role).map(([role, usd]) => (
            <div key={role} className="flex justify-between">
              <span className="text-zinc-500">{role}</span>
              <span className="tabular text-zinc-300">${usd.toFixed(4)}</span>
            </div>
          ))}
        </div>
      )}

      {days.length > 0 && (
        <div className="border-t border-zinc-800 pt-2">
          <div className="mb-1 text-xs text-zinc-500">Last 7 days</div>
          <div className="flex h-10 items-end gap-1">
            {days.map(([day, usd]) => (
              <div
                key={day}
                className="group relative flex-1"
                title={`${day}: $${usd.toFixed(4)}`}
              >
                <div
                  className={`w-full rounded-sm ${usd > 0 ? usageBarColor(cost.budget_usd > 0 ? usd / cost.budget_usd : 0) : "bg-zinc-800"}`}
                  style={{ height: `${Math.max(8, (usd / maxDay) * 100)}%` }}
                />
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="border-t border-zinc-800 pt-2 text-xs">
        <div className="flex justify-between">
          <span className="text-zinc-500">Month to date</span>
          <span className="tabular text-zinc-300">${cost.month_to_date_usd.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-zinc-500">Month-end projection</span>
          <span className={`tabular ${overMonthly ? "text-rose-400" : "text-zinc-300"}`}>
            ${cost.month_projection_usd.toFixed(2)}{" "}
            <span className="text-zinc-500">/ ${cost.monthly_limit_usd.toFixed(0)}</span>
          </span>
        </div>
        <p className="mt-1 text-[10px] text-zinc-500">
          The projection is roughly linear (month-to-date ÷ day × days in month) · Ollama calls cost $0.
        </p>
      </div>
    </div>
  );
}
