// Semantic design tokens — every widget speaks the SAME visual language.
// Rule: P&L/direction colour and system-health colour are separate concepts; never mix them.
// Colour never carries meaning alone (colour-blind friendly): always pair with arrow/icon/text.

/** Text tones — direction & status. */
export const tone = {
  positive: "text-emerald-400", // bullish / profit / above baseline
  negative: "text-rose-400", // bearish / loss / below baseline
  warning: "text-amber-400", // low sample size, stale, caution
  info: "text-sky-400", // info / link emphasis
  pending: "text-zinc-500", // DATA PENDING / neutral secondary text
} as const;

/** System health (is Argus awake?) — independent of the direction colour. */
export const healthDot = {
  ok: "bg-emerald-500",
  degraded: "bg-amber-500",
  fail: "bg-red-500",
} as const;

/** Severity badges (news label, alert severity) — SINGLE source. */
export const severityBadge: Record<string, string> = {
  critical: "bg-rose-500/20 text-rose-300",
  high: "bg-orange-500/20 text-orange-300",
  medium: "bg-amber-500/20 text-amber-300",
  low: "bg-zinc-700/50 text-zinc-400",
  info: "bg-zinc-700/50 text-zinc-400",
};

/** Target-agent badge (news routing). */
export const targetBadge = "bg-violet-500/20 text-violet-300";

/** "Could not be evaluated" badge. */
export const unevaluatedBadge = "bg-zinc-700/40 text-zinc-400";

/** Budget/progress bar colour (by usage ratio). */
export function usageBarColor(ratio: number): string {
  if (ratio >= 0.9) return "bg-rose-500";
  if (ratio >= 0.6) return "bg-amber-500";
  return "bg-emerald-500";
}

/** Versus-baseline delta colour (null → pending). */
export function deltaTone(delta: number | null | undefined): string {
  if (delta == null) return tone.pending;
  return delta >= 0 ? tone.positive : tone.negative;
}
