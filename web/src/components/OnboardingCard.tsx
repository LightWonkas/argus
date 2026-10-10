import { useEffect, useState } from "react";
import { api } from "../api";

const DISMISS_KEY = "argus.onboarding.dismissed";

interface CheckItem {
  done: boolean;
  label: string;
  note: string; // guides when done=false; nothing fabricated, just how to do it
}

/** "Wake Argus up" card: lists missing setup steps as ✅/⬜.
 *  The cockpit does not collect data (read-only); the card only shows the real
 *  state read from the existing APIs. Once dismissed it stays hidden via localStorage. */
export function OnboardingCard() {
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(DISMISS_KEY) === "1");
  const [checks, setChecks] = useState<CheckItem[] | null>(null);

  useEffect(() => {
    if (dismissed) return;
    let active = true;
    void Promise.allSettled([api.quotes(), api.getSettings(), api.news(1)]).then(
      ([quotes, settings, news]) => {
        if (!active) return;
        const q = quotes.status === "fulfilled" ? quotes.value : [];
        const s = settings.status === "fulfilled" ? settings.value : null;
        const n = news.status === "fulfilled" ? news.value : [];
        setChecks([
          {
            done: q.length > 0,
            label: "OHLCV data",
            note:
              q.length > 0
                ? `${q.length} series stored — keep python scripts/feed_once.py --loop 300 running to stay current`
                : "python scripts/feed_once.py --loop 300 (continuous) or python scripts/feed_once.py (one-off)",
          },
          {
            done: (s?.watch.watchlist.length ?? 0) > 0,
            label: "Watchlist",
            note:
              (s?.watch.watchlist.length ?? 0) > 0
                ? `${s?.watch.watchlist.length} symbols`
                : "Add a symbol under Settings → Watchlist",
          },
          {
            done: (s?.llm.live_providers.length ?? 0) > 0,
            label: "LLM provider",
            note:
              (s?.llm.live_providers.length ?? 0) > 0
                ? (s?.llm.live_providers ?? []).join(" + ")
                : "Start Ollama or enter an API key in Settings",
          },
          {
            done: s?.telegram.bot_token.set ?? false,
            label: "Telegram bot",
            note: s?.telegram.bot_token.set ? "connected" : "Settings → Telegram (optional)",
          },
          {
            done: n.length > 0,
            label: "News feed",
            note:
              n.length > 0
                ? "feed has items"
                : "Enter a key under Settings → Data Sources + run news_poller",
          },
        ]);
      },
    );
    return () => {
      active = false;
    };
  }, [dismissed]);

  if (dismissed || checks == null) return null;
  if (checks.every((c) => c.done)) return null; // everything is ready — the card is unnecessary

  function dismiss() {
    localStorage.setItem(DISMISS_KEY, "1");
    setDismissed(true);
  }

  return (
    <div className="mb-3 max-w-xl rounded-lg border border-zinc-700 bg-zinc-900/70 p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-zinc-200">👁 Wake Argus up</h3>
        <button
          onClick={dismiss}
          className="rounded px-1.5 py-0.5 text-xs text-zinc-500 hover:text-zinc-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500"
          title="Hide this card permanently"
        >
          close ✕
        </button>
      </div>
      <p className="mt-1 text-xs text-zinc-500">
        The cockpit does not collect data — the watch loop feeds it. Missing steps:
      </p>
      <ul className="mt-2 space-y-1.5 text-xs">
        {checks.map((c) => (
          <li key={c.label} className="flex items-baseline gap-2">
            <span aria-hidden>{c.done ? "✅" : "⬜"}</span>
            <span className={c.done ? "text-zinc-400" : "text-zinc-200"}>{c.label}</span>
            <span className="text-zinc-500">— {c.note}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
