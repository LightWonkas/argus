import { useEffect, useState } from "react";
import { api, type HealthResponse } from "../api";
import { PENDING } from "../format";

/** Component health (degraded-mode visibility) — a widget body that fetches its own data. */
export function HealthPanel() {
  const [health, setHealth] = useState<HealthResponse | null>(null);

  useEffect(() => {
    let active = true;
    const load = () =>
      api
        .health()
        .then((h) => active && setHealth(h))
        .catch(() => active && setHealth(null));
    void load();
    const t = setInterval(load, 30_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  return (
    <div className="p-3">
      {health ? (
        <ul className="space-y-2">
          {health.checks.map((c) => (
            <li key={c.name} className="flex items-center gap-3 text-sm">
              <span
                className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${
                  c.state === "ok"
                    ? "bg-emerald-500"
                    : c.state === "degraded"
                      ? "bg-amber-500"
                      : "bg-red-500"
                }`}
              />
              <span className="w-28 shrink-0 font-medium text-zinc-300">{c.name}</span>
              <span className="truncate text-zinc-400">{c.detail}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-zinc-500">{PENDING}</p>
      )}
    </div>
  );
}
