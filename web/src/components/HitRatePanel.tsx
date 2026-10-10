import { useEffect, useState } from "react";
import {
  api,
  type FeedbackSummary,
  type HitRateResponse,
  type MetricStat,
  type SeriesStats,
} from "../api";
import { PENDING } from "../format";
import { deltaTone, tone } from "../theme";
import { DataState } from "./DataState";

/** Hit rate — "how accurately does Argus measure itself", AGAINST THE BASELINE.
 *
 * A bare percentage means nothing on its own: each series (LLM analysis / confluence proposal)
 * is reported as "+X pts" against the random baseline; when n < min_n a "not statistically
 * significant" badge is shown (honesty — no bragging on a small sample).
 */

const SERIES_LABELS: Record<string, string> = {
  llm: "LLM analysis",
  proposal: "Confluence proposal",
};

const METRIC_LABELS: Record<string, string> = {
  touch: "Touch",
  close: "Close",
};

function DeltaBadge({ delta }: { delta: number | null }) {
  if (delta == null) return <span className="text-zinc-500">no baseline</span>;
  return (
    <span className={deltaTone(delta)}>
      {delta >= 0 ? "+" : ""}
      {delta.toFixed(1)} pts vs baseline
    </span>
  );
}

function MetricRow({
  name,
  stat,
  baseline,
  delta,
}: {
  name: string;
  stat: MetricStat;
  baseline: MetricStat;
  delta: number | null;
}) {
  if (stat.n === 0)
    return (
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="text-zinc-500">{METRIC_LABELS[name] ?? name}</span>
        <span className="text-zinc-500">{PENDING}</span>
      </div>
    );
  return (
    <div className="flex items-baseline justify-between gap-2 text-xs">
      <span className="text-zinc-500">{METRIC_LABELS[name] ?? name}</span>
      <span className="tabular text-right">
        <DeltaBadge delta={delta} />{" "}
        <span className="text-zinc-500">
          (system {stat.pct ?? "—"}%
          {baseline.pct != null ? ` · random ${baseline.pct}%` : ""} · n={stat.n})
        </span>
      </span>
    </div>
  );
}

function SeriesCard({ name, s, minN }: { name: string; s: SeriesStats; minN: number }) {
  const hasAny = s.touch.n > 0 || s.close.n > 0;
  return (
    <div className="rounded border border-zinc-800 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-sm text-zinc-300">{SERIES_LABELS[name] ?? name}</span>
        {hasAny && s.low_n && (
          <span className="rounded bg-amber-950 px-1.5 py-0.5 text-[10px] text-amber-400">
            n&lt;{minN} — not statistically significant
          </span>
        )}
      </div>
      {!hasAny ? (
        <p className="text-xs text-zinc-500">No final outcomes yet ({PENDING})</p>
      ) : (
        <div className="space-y-1.5">
          <MetricRow
            name="touch"
            stat={s.touch}
            baseline={s.baselines.random?.touch ?? { hit: 0, n: 0, pct: null }}
            delta={s.delta_pts.touch ?? null}
          />
          <MetricRow
            name="close"
            stat={s.close}
            baseline={s.baselines.random?.close ?? { hit: 0, n: 0, pct: null }}
            delta={s.delta_pts.close ?? null}
          />
        </div>
      )}
    </div>
  );
}

export function HitRatePanel() {
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
        .catch(() => active && setFb(null)); // if the endpoint is missing the row is silently hidden
    };
    void load();
    const t = setInterval(load, 30_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  if (hr == null) return <p className="p-4 text-zinc-500">{PENDING}</p>;

  const series = hr.series ?? {};
  const names = ["llm", "proposal"].filter((n) => series[n]);
  const noData =
    hr.total === 0 &&
    names.every((n) => series[n].touch.n === 0 && series[n].close.n === 0);

  return (
    <div className="space-y-2 p-4">
      {noData ? (
        <DataState
          reason="no resolved analyses yet — the evaluator scores them once their horizons elapse"
          hint="python scripts/feed_once.py"
        />
      ) : (
        names.map((n) => <SeriesCard key={n} name={n} s={series[n]} minN={hr.min_n} />)
      )}
      {fb != null && fb.total > 0 && (
        <div className="flex items-baseline justify-between rounded border border-zinc-800 px-3 py-2 text-xs">
          <span className="text-zinc-500">Alert quality (👍 votes, {fb.days}d)</span>
          <span className="tabular">
            <span
              className={
                fb.useful_pct != null && fb.useful_pct >= 50 ? tone.positive : tone.warning
              }
            >
              {fb.useful_pct}%
            </span>{" "}
            <span className="text-zinc-500">(n={fb.total})</span>
          </span>
        </div>
      )}
      <p className="text-[10px] text-zinc-500">
        Threshold: k×ATR (volatility-normalized) · baselines: random/bullish/momentum · this is
        self-monitoring, not proof of an edge.
      </p>
    </div>
  );
}
