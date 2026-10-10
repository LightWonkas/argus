import { useEffect, useState } from "react";
import { api, type FeedbackSummary, type HitRateResponse } from "../api";
import { deltaTone, tone } from "../theme";
import { CostPanel } from "./CostPanel";
import { DataState } from "./DataState";
import { HealthPanel } from "./HealthPanel";
import { HitRatePanel } from "./HitRatePanel";

const SERIES_LABELS: Record<string, string> = {
  llm: "LLM analysis",
  proposal: "Confluence proposal",
};
const METRIC_LABELS: Record<string, string> = { touch: "Touch", close: "Close" };
const BASELINE_LABELS: Record<string, string> = {
  random: "Random (coin flip)",
  bullish: "Always-bullish",
  momentum: "Momentum",
};

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900">
      <h2 className="border-b border-zinc-800 px-4 py-2.5 text-sm font-semibold text-zinc-200">
        {title}
      </h2>
      {children}
    </section>
  );
}

/** Baseline comparison table — the methodology is the defendant, the baselines are the jury.
 *  The system only speaks relative to a baseline; the table shows all three (not just random). */
function BaselineTable({ hr }: { hr: HitRateResponse }) {
  const rows: {
    series: string;
    metric: string;
    sys: number | null;
    n: number;
    bases: Record<string, number | null>;
  }[] = [];
  for (const [name, s] of Object.entries(hr.series ?? {})) {
    for (const metric of ["touch", "close"] as const) {
      const stat = s[metric];
      if (stat.n === 0) continue;
      rows.push({
        series: SERIES_LABELS[name] ?? name,
        metric: METRIC_LABELS[metric],
        sys: stat.pct,
        n: stat.n,
        bases: Object.fromEntries(
          Object.entries(s.baselines).map(([b, v]) => [b, v[metric].pct]),
        ),
      });
    }
  }
  if (rows.length === 0)
    return (
      <DataState
        reason="no final outcomes yet — the evaluator scores them once their horizons elapse"
        hint="python scripts/feed_once.py"
      />
    );
  const baseNames = ["random", "bullish", "momentum"].filter((b) =>
    rows.some((r) => r.bases[b] != null),
  );
  return (
    <div className="overflow-x-auto p-3">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-zinc-500">
            <th className="px-2 py-1.5 font-medium">Series · Metric</th>
            <th className="tabular px-2 py-1.5 text-right font-medium">System</th>
            {baseNames.map((b) => (
              <th key={b} className="tabular px-2 py-1.5 text-right font-medium">
                {BASELINE_LABELS[b] ?? b}
              </th>
            ))}
            <th className="tabular px-2 py-1.5 text-right font-medium">n</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const randomPct = r.bases.random;
            const delta = r.sys != null && randomPct != null ? r.sys - randomPct : null;
            return (
              <tr key={`${r.series}-${r.metric}`} className="border-t border-zinc-800/60">
                <td className="px-2 py-1.5 text-zinc-300">
                  {r.series} · {r.metric}
                </td>
                <td className={`tabular px-2 py-1.5 text-right ${deltaTone(delta)}`}>
                  {r.sys != null ? `${r.sys}%` : "—"}
                </td>
                {baseNames.map((b) => (
                  <td key={b} className="tabular px-2 py-1.5 text-right text-zinc-400">
                    {r.bases[b] != null ? `${r.bases[b]}%` : "—"}
                  </td>
                ))}
                <td className="tabular px-2 py-1.5 text-right text-zinc-500">{r.n}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-2 px-2 text-[10px] text-zinc-500">
        The system cell is colored relative to the random baseline. All three baselines are shown —
        a methodology that cannot beat them stays in the dock.
      </p>
    </div>
  );
}

/** User value: per-symbol breakdown of 👍/👎 votes. */
function FeedbackSection({ fb }: { fb: FeedbackSummary | null }) {
  if (fb == null || fb.total === 0)
    return (
      <DataState
        reason="no feedback yet — vote with the 👍/👎 under an alert in Telegram"
      />
    );
  const symbols = Object.entries(fb.by_symbol).sort((a, b) => b[1].total - a[1].total);
  return (
    <div className="space-y-2 p-4 text-xs">
      <div className="flex items-baseline justify-between">
        <span className="text-zinc-400">Overall ({fb.days} days)</span>
        <span className="tabular">
          <span
            className={fb.useful_pct != null && fb.useful_pct >= 50 ? tone.positive : tone.warning}
          >
            {fb.useful_pct ?? "—"}% useful
          </span>{" "}
          <span className="text-zinc-500">
            ({fb.useful}/{fb.total})
          </span>
        </span>
      </div>
      {symbols.length > 0 && (
        <div className="space-y-1 border-t border-zinc-800 pt-2">
          {symbols.map(([sym, c]) => (
            <div key={sym} className="flex items-baseline justify-between">
              <span className="text-zinc-300">{sym}</span>
              <span className="tabular text-zinc-400">
                👍 {c.useful}/{c.total}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Performance page — the showcase of the measure-first philosophy: hit rate (vs baseline) +
 *  baseline comparison + user value (👍/👎) + cost + component health in one place. */
export function PerformancePage({ onBack }: { onBack: () => void }) {
  const [hr, setHr] = useState<HitRateResponse | null>(null);
  const [fb, setFb] = useState<FeedbackSummary | null>(null);

  useEffect(() => {
    let active = true;
    const load = () => {
      api
        .hitrate()
        .then((d) => active && setHr(d))
        .catch(() => active && setHr(null));
      api
        .feedbackSummary()
        .then((d) => active && setFb(d))
        .catch(() => active && setFb(null));
    };
    void load();
    const t = setInterval(load, 60_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  return (
    <main className="mx-auto max-w-5xl px-4 py-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Performance</h1>
          <p className="text-xs text-zinc-500">
            Argus's self-monitoring: hit rate vs baseline · user value · cost · health.
            This is self-monitoring, not proof of an edge.
          </p>
        </div>
        <button
          onClick={onBack}
          className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          ← Back to cockpit
        </button>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="🎯 Hit rate (vs baseline)">
          <HitRatePanel />
        </Card>
        <Card title="⚖ Baseline comparison">
          {hr ? (
            <BaselineTable hr={hr} />
          ) : (
            <p className="p-4 text-sm text-zinc-500">Loading…</p>
          )}
        </Card>
        <Card title="👍 User value (feedback)">
          <FeedbackSection fb={fb} />
        </Card>
        <Card title="💸 Cost (LLM budget)">
          <CostPanel />
        </Card>
        <Card title="🩺 Component health">
          <HealthPanel />
        </Card>
      </div>

      <p className="mt-8 text-center text-xs text-zinc-500">
        All metrics come from the deterministic evaluator · Not financial advice.
      </p>
    </main>
  );
}
