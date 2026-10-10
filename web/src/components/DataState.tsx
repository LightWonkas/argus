// Honesty states — one uniform visual language.
// Argus's signature states: DATA PENDING · STALE · NOT EVALUATED · FLYING BLIND.
// The "no fabrication" philosophy is the visual identity: even the empty state is designed
// and says why.

import { PENDING } from "../format";

export type DataStateKind = "pending" | "stale" | "unevaluated" | "blind";

const KINDS: Record<DataStateKind, { icon: string; label: string; cls: string }> = {
  pending: { icon: "⏳", label: PENDING, cls: "text-zinc-400" },
  stale: { icon: "🕓", label: "STALE", cls: "text-amber-400" },
  unevaluated: { icon: "🚫", label: "NOT EVALUATED", cls: "text-zinc-400" },
  blind: { icon: "🙈", label: "FLYING BLIND", cls: "text-rose-400" },
};

/** Empty/missing data state — icon + label + optional reason (visible text, not a tooltip). */
export function DataState({
  kind = "pending",
  reason,
  hint,
  className = "",
}: {
  kind?: DataStateKind;
  reason?: string;
  hint?: string; // e.g. "python scripts/feed_once.py" — how to fill it
  className?: string;
}) {
  const k = KINDS[kind];
  return (
    <div className={`flex flex-col items-center gap-1 p-6 text-center text-sm ${className}`}>
      <div className="flex items-center gap-2">
        <span aria-hidden>{k.icon}</span>
        <span className={`font-medium ${k.cls}`}>{k.label}</span>
      </div>
      {reason && <span className="text-xs text-zinc-500">{reason}</span>}
      {hint && (
        <code className="mt-1 rounded bg-zinc-800 px-2 py-0.5 text-[11px] text-zinc-400">
          {hint}
        </code>
      )}
    </div>
  );
}
