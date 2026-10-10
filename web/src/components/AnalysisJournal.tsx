import { useEffect, useState } from "react";
import { api, type AnalysisDetail, type AnalysisListItem } from "../api";
import { PENDING, biasBadge, evidenceColor, istanbul } from "../format";

/** Analysis journal: list + full analysis on click (scenarios, "not financial advice"). */
export function AnalysisJournal() {
  const [items, setItems] = useState<AnalysisListItem[] | null>(null);
  const [open, setOpen] = useState<AnalysisDetail | null>(null);

  useEffect(() => {
    let active = true;
    const load = () =>
      api
        .analyses(20)
        .then((d) => active && setItems(d))
        .catch(() => active && setItems([]));
    void load();
    const t = setInterval(load, 30_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  async function toggle(id: number) {
    if (open?.id === id) {
      setOpen(null);
      return;
    }
    try {
      setOpen(await api.analysis(id));
    } catch {
      setOpen(null);
    }
  }

  return (
    <div className="p-3">
      {items === null ? (
        <p className="text-zinc-500">Loading…</p>
      ) : items.length === 0 ? (
        <p className="text-zinc-500">{PENDING} — no analyses yet</p>
      ) : (
        <ul className="divide-y divide-zinc-800">
          {items.map((it) => {
            const b = biasBadge(it.bias);
            const isOpen = open?.id === it.id;
            return (
              <li key={it.id}>
                <button
                  onClick={() => void toggle(it.id)}
                  className="flex w-full items-center gap-3 py-2.5 text-left text-sm hover:bg-zinc-800/40"
                >
                  <span className="tabular w-24 shrink-0 text-xs text-zinc-500">
                    {istanbul(it.created_at)}
                  </span>
                  <span className="w-24 font-medium text-zinc-200">{it.symbol}</span>
                  <span
                    className={`rounded px-1.5 py-0.5 text-xs font-semibold ${evidenceColor(it.evidence_tier)}`}
                  >
                    [{it.evidence_tier}]
                  </span>
                  <span className={`w-24 text-xs ${b.cls}`}>{b.label}</span>
                  <span className="tabular w-12 text-xs text-zinc-400">{it.confidence_pct}%</span>
                  <span className="flex-1 truncate text-zinc-400">{it.summary}</span>
                </button>

                {isOpen && open && (
                  <div className="mb-3 rounded-lg bg-zinc-800/50 p-4 text-sm">
                    <p className="text-zinc-200">{open.analysis.summary}</p>
                    <p className="mt-2 text-xs text-zinc-400">
                      <span className="text-zinc-500">Invalidation:</span>{" "}
                      {open.analysis.invalidation}
                    </p>
                    {open.analysis.scenarios.length > 0 && (
                      <div className="mt-3 space-y-1">
                        <div className="text-xs font-medium text-zinc-500">Scenarios</div>
                        {open.analysis.scenarios.map((s, i) => (
                          <div key={i} className="text-xs text-zinc-300">
                            <span className="tabular text-zinc-400">{s.probability_pct}%</span>{" "}
                            <span className="font-medium">{s.name}</span> — {s.detail}
                          </div>
                        ))}
                      </div>
                    )}
                    <div className="mt-3 flex items-center justify-between text-xs text-zinc-500">
                      <span>model: {open.analysis.model}</span>
                      <span className="rounded bg-amber-500/10 px-2 py-0.5 text-amber-500/80">
                        {open.analysis.disclaimer}
                      </span>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
