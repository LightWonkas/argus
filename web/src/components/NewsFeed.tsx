import { useEffect, useState } from "react";
import { api, type NewsResponseItem } from "../api";
import { useDashboard } from "../dashboard/dashboardState";
import { istanbul } from "../format";
import { severityBadge, targetBadge, unevaluatedBadge } from "../theme";
import { DataState } from "./DataState";

/** Router label badges: importance + target agent; explicit not-evaluated state. */
function RouteBadges({ n }: { n: NewsResponseItem }) {
  if (n.route_status == null) return null; // not labelled → no badge (silent)
  if (n.route_status !== "ok")
    return (
      <span className={`rounded px-1.5 py-0.5 text-[10px] ${unevaluatedBadge}`}>
        not evaluated
      </span>
    );
  return (
    <>
      {n.importance && (
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
            severityBadge[n.importance] ?? severityBadge.low
          }`}
        >
          {n.importance}
        </span>
      )}
      {n.relevant && n.target_agent && n.target_agent !== "none" && (
        <span className={`rounded px-1.5 py-0.5 text-[10px] ${targetBadge}`}>
          → {n.target_agent}
        </span>
      )}
    </>
  );
}

/** News/KAP disclosure feed: source badge · title · asset · time · link. */
export function NewsFeed() {
  const { activeSymbol } = useDashboard();
  const [news, setNews] = useState<NewsResponseItem[] | null>(null);
  // 🔗 active-symbol filter: only the active symbol + symbol-less (macro/general) news.
  const [linkedOnly, setLinkedOnly] = useState(false);

  useEffect(() => {
    let active = true;
    const load = () =>
      api
        .news(40)
        .then((n) => active && setNews(n))
        .catch(() => active && setNews([]));
    void load();
    const t = setInterval(load, 30_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  const shown =
    news == null
      ? null
      : linkedOnly && activeSymbol
        ? news.filter((n) => n.symbol == null || n.symbol === activeSymbol)
        : news;

  return (
    <div className="p-3">
      {activeSymbol && (
        <div className="mb-2 flex gap-1">
          <button
            onClick={() => setLinkedOnly(false)}
            className={`rounded px-2 py-0.5 text-xs focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 ${
              !linkedOnly ? "bg-zinc-700 text-zinc-100" : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            all
          </button>
          <button
            onClick={() => setLinkedOnly(true)}
            title="Only the active symbol + macro/general news"
            className={`rounded px-2 py-0.5 text-xs focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 ${
              linkedOnly ? "bg-sky-500/20 text-sky-300" : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            🔗 {activeSymbol}
          </button>
        </div>
      )}
      {shown === null ? (
        <p className="text-sm text-zinc-500">Loading…</p>
      ) : shown.length === 0 ? (
        linkedOnly && news && news.length > 0 ? (
          <p className="text-sm text-zinc-500">No news for {activeSymbol} — check "all".</p>
        ) : (
          <DataState reason="no news yet" hint="python scripts/news_poller.py" />
        )
      ) : (
        <ul className="space-y-3">
          {shown.map((n) => {
            const isKap = n.kind === "disclosure";
            const Wrapper = n.url ? "a" : "div";
            return (
              <li key={`${n.source}-${n.title}-${n.published_at}`}>
                <Wrapper
                  {...(n.url ? { href: n.url, target: "_blank", rel: "noreferrer" } : {})}
                  className={`block rounded-lg border border-zinc-800 p-2.5 ${
                    n.url ? "hover:border-zinc-600" : ""
                  }`}
                >
                  <div className="mb-1 flex items-center gap-2">
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                        isKap
                          ? "bg-sky-500/20 text-sky-300"
                          : "bg-zinc-700/50 text-zinc-300"
                      }`}
                    >
                      {isKap ? "KAP" : n.source}
                    </span>
                    {n.symbol && <span className="text-xs font-medium text-zinc-300">{n.symbol}</span>}
                    <RouteBadges n={n} />
                    <span className="tabular ml-auto text-[10px] text-zinc-500">
                      {istanbul(n.published_at)}
                    </span>
                  </div>
                  <p className="text-sm leading-snug text-zinc-200">{n.title}</p>
                </Wrapper>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
