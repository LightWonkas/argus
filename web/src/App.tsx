import { Suspense, lazy, useCallback, useEffect, useState } from "react";
import { useAlertBadge } from "./alertBadge";
import { ApiError, api, type HealthResponse, type StatusResponse } from "./api";
import { Login } from "./components/Login";
import { Sidebar, type NavView } from "./components/Sidebar";
import { StatusBar } from "./components/StatusBar";
import { CommandPalette } from "./dashboard/CommandPalette";
import { CopilotRail } from "./dashboard/CopilotRail";
import { DashboardProvider } from "./dashboard/context";
import { DashboardView } from "./dashboard/DashboardView";

// Settings/model pages are rarely opened — load them separately from the main bundle.
const SettingsPage = lazy(() =>
  import("./components/SettingsPage").then((m) => ({ default: m.SettingsPage })),
);
const ModelsPage = lazy(() =>
  import("./components/ModelsPage").then((m) => ({ default: m.ModelsPage })),
);
const PerformancePage = lazy(() =>
  import("./components/PerformancePage").then((m) => ({ default: m.PerformancePage })),
);
const TradingPage = lazy(() =>
  import("./components/TradingPage").then((m) => ({ default: m.TradingPage })),
);

const pageFallback = <div className="p-6 text-sm text-zinc-500">Loading…</div>;

type Auth = "checking" | "in" | "out";

const POLL_MS = 15_000; // slow panels poll every 15–30 s

export function App() {
  const [auth, setAuth] = useState<Auth>("checking");
  const [view, setView] = useState<NavView>("panel");
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [stale, setStale] = useState(false);
  // Critical alert watch: title badge while in a background tab + notification (if permitted).
  useAlertBadge(auth === "in");

  const refresh = useCallback(async () => {
    try {
      const [s, h] = await Promise.all([api.status(), api.health()]);
      setStatus(s);
      setHealth(h);
      setStale(false);
      setAuth("in");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAuth("out");
      } else {
        setStale(true); // connection lost — never show stale data silently
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (auth !== "in") return;
    const t = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(t);
  }, [auth, refresh]);

  async function logout() {
    await api.logout();
    setStatus(null);
    setHealth(null);
    setAuth("out");
  }

  if (auth === "checking") {
    return (
      <div className="flex min-h-screen items-center justify-center text-zinc-500">
        Loading…
      </div>
    );
  }

  if (auth === "out") {
    return <Login onSuccess={() => void refresh()} />;
  }

  return (
    <DashboardProvider>
      <div className="flex h-screen overflow-hidden bg-zinc-950 text-zinc-100">
      {/* Left — navigation rail */}
      <div className="hidden md:flex">
        <Sidebar view={view} onNav={setView} onLogout={() => void logout()} />
      </div>

      {/* Center + right */}
      <div className="flex flex-1 flex-col overflow-hidden">
        <StatusBar status={status} health={health} />

        {stale && (
          <div className="shrink-0 bg-amber-500/10 px-4 py-1.5 text-center text-sm text-amber-400">
            Not live — reconnecting…
          </div>
        )}

        {view === "trading" ? (
          <div className="flex-1 overflow-y-auto">
            <Suspense fallback={pageFallback}>
              <TradingPage onBack={() => setView("panel")} />
            </Suspense>
          </div>
        ) : view === "settings" ? (
          <div className="flex-1 overflow-y-auto">
            <Suspense fallback={pageFallback}>
              <SettingsPage onBack={() => setView("panel")} />
            </Suspense>
          </div>
        ) : view === "models" ? (
          <div className="flex-1 overflow-y-auto">
            <Suspense fallback={pageFallback}>
              <ModelsPage onBack={() => setView("panel")} />
            </Suspense>
          </div>
        ) : view === "performance" ? (
          <div className="flex-1 overflow-y-auto">
            <Suspense fallback={pageFallback}>
              <PerformancePage onBack={() => setView("panel")} />
            </Suspense>
          </div>
        ) : (
          <div className="flex flex-1 overflow-hidden">
            {/* Center — movable widget grid */}
            <main className="min-w-0 flex-1 overflow-hidden">
              <DashboardView />
            </main>

            {/* Right — Argus Copilot rail (bound to the active symbol) */}
            <aside className="hidden w-[400px] shrink-0 border-l border-zinc-800 p-3 xl:block">
              <CopilotRail />
            </aside>
          </div>
        )}
      </div>
      </div>

      {/* Global command palette (Cmd/Ctrl+K) */}
      <CommandPalette onNav={setView} />
    </DashboardProvider>
  );
}
