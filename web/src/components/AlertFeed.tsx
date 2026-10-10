import { useEffect, useState } from "react";
import { api, type AlertItem } from "../api";
import { istanbul, severityColor } from "../format";
import { DataState } from "./DataState";
import { FeedbackButtons } from "./FeedbackButtons";

const SEVERITIES = ["", "critical", "high", "medium", "low"];
const SEV_LABEL: Record<string, string> = {
  "": "all",
  critical: "critical",
  high: "high",
  medium: "medium",
  low: "low",
};

/** Live alert feed (polling for now). */
export function AlertFeed() {
  const [alerts, setAlerts] = useState<AlertItem[] | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let active = true;
    const load = () =>
      api
        .alerts(50)
        .then((a) => active && setAlerts(a))
        .catch(() => active && setAlerts([]));
    void load();
    const t = setInterval(load, 15_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  const shown = alerts?.filter((a) => !filter || a.severity === filter) ?? null;

  return (
    <div className="p-3">
      <div className="mb-3 flex flex-wrap gap-1">
        {SEVERITIES.map((s) => (
          <button
            key={s}
            onClick={() => setFilter(s)}
            className={`rounded px-2 py-0.5 text-xs ${
              filter === s ? "bg-zinc-700 text-zinc-100" : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {SEV_LABEL[s]}
          </button>
        ))}
      </div>

      {shown === null ? (
        <p className="text-zinc-500">Loading…</p>
      ) : shown.length === 0 ? (
        filter ? (
          <p className="text-sm text-zinc-500">No alerts at this severity.</p>
        ) : (
          <DataState
            reason="no alerts yet — they appear once the watch loop fires a trigger"
            hint="python -m argus --watch"
          />
        )
      ) : (
        <ul className="space-y-2">
          {shown.map((a, i) => (
            <li
              key={`${a.analysis_id}-${a.kind}-${i}`}
              className={`flex items-center gap-3 rounded-lg border px-3 py-2 text-sm ${severityColor(a.severity)}`}
            >
              <span className="rounded bg-black/20 px-1.5 py-0.5 text-xs uppercase">{a.severity}</span>
              <span className="font-medium text-zinc-200">{a.symbol}</span>
              <span className="text-zinc-400">{a.kind}</span>
              <span className="flex-1 truncate text-zinc-300">{a.message}</span>
              <FeedbackButtons
                vote={{
                  ref_type: "analysis",
                  ref_id: String(a.analysis_id),
                  symbol: a.symbol,
                  severity: a.severity,
                }}
              />
              <span className="tabular shrink-0 text-xs text-zinc-500">{istanbul(a.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
