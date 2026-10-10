import { ArgusLogo } from "./ArgusLogo";

export type NavView = "panel" | "trading" | "performance" | "models" | "settings";

/** Left navigation sidebar (OpenBB-style cockpit rail layout). */
export function Sidebar({
  view,
  onNav,
  onLogout,
}: {
  view: NavView;
  onNav: (v: NavView) => void;
  onLogout: () => void;
}) {
  const item = (active: boolean) =>
    `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
      active
        ? "bg-zinc-800 text-zinc-100"
        : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
    }`;

  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-zinc-800 bg-zinc-950">
      {/* Logo */}
      <div className="flex items-center gap-2.5 border-b border-zinc-800 px-4 py-4">
        <ArgusLogo size={28} pulse />
        <div className="leading-tight">
          <div className="font-semibold tracking-wide text-zinc-100">Argus</div>
          <div className="text-[10px] tracking-wide text-zinc-500">24/7 watch</div>
        </div>
      </div>

      {/* Search — opens the command palette (Cmd/Ctrl+K) */}
      <div className="px-3 pt-3">
        <button
          onClick={() => window.dispatchEvent(new Event("argus:palette"))}
          className="flex w-full items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs text-zinc-500 hover:border-zinc-700 hover:text-zinc-300"
        >
          <span>🔍</span>
          <span>Search…</span>
          <span className="ml-auto rounded border border-zinc-700 px-1 text-[10px] text-zinc-500">
            ⌘K
          </span>
        </button>
      </div>

      {/* Navigation */}
      <nav className="flex-1 space-y-1 px-3 pt-4">
        <button
          onClick={() => onNav("panel")}
          className={item(view === "panel")}
        >
          <span>📊</span> Watch Panel
        </button>
        <button
          onClick={() => onNav("trading")}
          className={item(view === "trading")}
        >
          <span>🛡</span> Shadow Simulation
        </button>
        <button
          onClick={() => onNav("performance")}
          className={item(view === "performance")}
        >
          <span>🎯</span> Performance
        </button>
        <button
          onClick={() => onNav("models")}
          className={item(view === "models")}
        >
          <span>🧠</span> Models
        </button>
        <button
          onClick={() => onNav("settings")}
          className={item(view === "settings")}
        >
          <span>⚙</span> Settings
        </button>

        <div className="px-3 pb-1 pt-5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
          Tip
        </div>
        <p className="px-3 text-[11px] leading-relaxed text-zinc-500">
          Drag widgets by their header and resize them from the corner; add more with
          “＋ Widget”. Pin the layout with “🔒 Locked”. The layout is stored in the browser.
        </p>
      </nav>

      {/* Bottom: sign out + version */}
      <div className="border-t border-zinc-800 p-3">
        <button
          onClick={onLogout}
          className="w-full rounded-lg border border-zinc-800 px-3 py-1.5 text-sm text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
        >
          Sign out
        </button>
        <p className="mt-3 text-center text-[10px] text-zinc-500">
          Argus v0.1.0 · decision support
        </p>
      </div>
    </aside>
  );
}
