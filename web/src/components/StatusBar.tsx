import { useEffect, useState } from "react";
import { api, type HealthResponse, type HitRateResponse, type StatusResponse } from "../api";
import { deltaTone, healthDot, tone, usageBarColor } from "../theme";
import { ArgusLogo } from "./ArgusLogo";

const PENDING = "DATA PENDING";

function istanbulTime(): string {
  return new Intl.DateTimeFormat("en-US", {
    hourCycle: "h23",
    timeZone: "Europe/Istanbul",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date());
}

/** "At a glance" strip: is everything OK?
 *  Wakefulness · budget (from the persistent ledger) · hit rate VS BASELINE · source status. */
export function StatusBar({
  status,
  health,
}: {
  status: StatusResponse | null;
  health: HealthResponse | null;
}) {
  const [clock, setClock] = useState(istanbulTime);
  const [hr, setHr] = useState<HitRateResponse | null>(null);
  const [notifPerm, setNotifPerm] = useState<NotificationPermission | "unsupported">(
    typeof Notification === "undefined" ? "unsupported" : Notification.permission,
  );

  useEffect(() => {
    const t = setInterval(() => setClock(istanbulTime()), 1000);
    return () => clearInterval(t);
  }, []);

  // Hit rate vs baseline (touch metric, llm series) — a bare percentage never reaches the strip.
  useEffect(() => {
    let active = true;
    const load = () =>
      api
        .hitrate()
        .then((d) => active && setHr(d))
        .catch(() => active && setHr(null));
    void load();
    const t = setInterval(load, 60_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  const blind = status?.degraded_sources ?? [];
  const allEyesOpen = health?.state === "ok" && blind.length === 0;
  const eyeColor = allEyesOpen
    ? healthDot.ok
    : health?.state === "fail"
      ? healthDot.fail
      : healthDot.degraded;
  const eyeLabel = allEyesOpen ? "Awake" : health?.state === "fail" ? "Faulty" : "Partially blind";

  const spent =
    status && status.budget_total_usd != null && status.budget_remaining_usd != null
      ? status.budget_total_usd - status.budget_remaining_usd
      : null;
  const budgetRatio =
    spent != null && status?.budget_total_usd ? spent / status.budget_total_usd : null;

  const llm = hr?.series?.llm;
  const delta = llm?.delta_pts?.touch ?? llm?.delta_pts?.close ?? null;
  const deltaN = llm ? Math.max(llm.touch.n, llm.close.n) : 0;

  const sep = <span className="h-4 w-px bg-zinc-800" aria-hidden />;

  return (
    <header className="shrink-0 border-b border-zinc-800 bg-gradient-to-b from-zinc-900/60 to-zinc-950">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-2.5 text-sm">
        {/* Brand + wakefulness */}
        <div className="flex items-center gap-2.5">
          <ArgusLogo size={22} pulse />
          <span className="font-semibold tracking-wide text-zinc-100">Argus</span>
          <span className="relative flex items-center gap-1.5">
            <span className={`inline-block h-2 w-2 rounded-full ${eyeColor} animate-pulse`} />
            <span className="text-zinc-400">{eyeLabel}</span>
          </span>
        </div>

        {sep}

        {/* Budget — from the persistent cost ledger (the total across all processes) */}
        <div className="flex items-center gap-2" title="Daily LLM budget (tokens + web search)">
          <span aria-hidden>💸</span>
          {budgetRatio == null ? (
            <span className={tone.pending}>{PENDING}</span>
          ) : (
            <span className="flex items-center gap-2">
              <span className="h-1.5 w-24 overflow-hidden rounded-full bg-zinc-700">
                <span
                  className={`block h-full ${usageBarColor(budgetRatio)}`}
                  style={{ width: `${Math.min(100, budgetRatio * 100)}%` }}
                />
              </span>
              <span className="tabular text-zinc-300">
                ${spent?.toFixed(2)}
                <span className="text-zinc-500">/{status?.budget_total_usd?.toFixed(2)}</span>
              </span>
            </span>
          )}
        </div>

        {sep}

        {/* Hit rate — VS BASELINE (touch; points relative to the random baseline) */}
        <div className="flex items-center gap-1.5" title="Hit rate vs the random baseline (touch)">
          <span aria-hidden>🎯</span>
          {delta == null || deltaN === 0 ? (
            <span className={tone.pending}>no measurements yet</span>
          ) : (
            <>
              <span className={`tabular ${llm?.low_n ? tone.warning : deltaTone(delta)}`}>
                {delta >= 0 ? "+" : ""}
                {delta.toFixed(1)} pts
              </span>
              <span className="tabular text-xs text-zinc-500">(n={deltaN})</span>
            </>
          )}
        </div>

        {sep}

        {/* Source visibility */}
        <div
          className="flex items-center gap-1.5"
          title={blind.length ? `Blind: ${blind.join(", ")}` : "All data sources are visible"}
        >
          <span aria-hidden>👁</span>
          {blind.length > 0 ? (
            <span className={tone.warning}>{blind.length} sources blind</span>
          ) : (
            <span className="text-zinc-300">sources online</span>
          )}
        </div>

        <div className="ml-auto flex items-center gap-3 text-zinc-400">
          {notifPerm !== "unsupported" && notifPerm !== "denied" && (
            <button
              onClick={() => {
                if (notifPerm === "default")
                  void Notification.requestPermission().then(setNotifPerm);
              }}
              title={
                notifPerm === "granted"
                  ? "Critical alert notifications are on"
                  : "Allow browser notifications for critical alerts"
              }
              className={`rounded px-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 ${
                notifPerm === "granted" ? "text-sky-400" : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              🔔
            </button>
          )}
          {status && (
            <span className="rounded-full border border-zinc-700 bg-zinc-800/60 px-2.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
              {status.env} · v{status.version}
            </span>
          )}
          <span className="tabular text-zinc-300" title="Europe/Istanbul time">
            🕐 {clock}
          </span>
        </div>
      </div>
    </header>
  );
}
