// Command palette (Cmd/Ctrl+K) — a global search for jumping quickly to a symbol or page.
// Opens with Cmd/Ctrl+K or the "argus:palette" event on `window` (fired by the sidebar search box).

import { useEffect, useMemo, useRef, useState } from "react";
import type { NavView } from "../components/Sidebar";
import { useDashboard } from "./dashboardState";

type Cmd =
  | { kind: "symbol"; symbol: string }
  | { kind: "page"; view: NavView; label: string };

const PAGES: { view: NavView; label: string }[] = [
  { view: "panel", label: "Watch Panel" },
  { view: "trading", label: "Shadow Simulation" },
  { view: "performance", label: "Performance" },
  { view: "models", label: "Models" },
  { view: "settings", label: "Settings" },
];

export function CommandPalette({ onNav }: { onNav: (v: NavView) => void }) {
  const { symbols, setActiveSymbol } = useDashboard();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Global shortcut + sidebar event.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      } else if (e.key === "Escape") {
        setOpen(false);
      }
    };
    const onEvt = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("argus:palette", onEvt);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("argus:palette", onEvt);
    };
  }, []);

  // Reset + focus on open.
  useEffect(() => {
    if (open) {
      setQuery("");
      setSel(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const results = useMemo<Cmd[]>(() => {
    const q = query.trim().toLowerCase();
    const pages: Cmd[] = PAGES.filter((p) => p.label.toLowerCase().includes(q)).map(
      (p) => ({ kind: "page", view: p.view, label: p.label }),
    );
    const syms: Cmd[] = symbols
      .filter((s) => s.toLowerCase().includes(q))
      .slice(0, 50)
      .map((s) => ({ kind: "symbol", symbol: s }));
    return [...syms, ...pages];
  }, [query, symbols]);

  // Keep the selection within range.
  useEffect(() => {
    if (sel >= results.length) setSel(0);
  }, [results, sel]);

  function run(cmd: Cmd) {
    if (cmd.kind === "symbol") {
      setActiveSymbol(cmd.symbol);
      onNav("panel"); // chart/quote live on the panel
    } else {
      onNav(cmd.view);
    }
    setOpen(false);
  }

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 pt-[15vh]"
      onClick={() => setOpen(false)}
    >
      <div
        className="w-full max-w-lg overflow-hidden rounded-xl border border-zinc-700 bg-zinc-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setSel((s) => Math.min(s + 1, results.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setSel((s) => Math.max(s - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              const c = results[sel];
              if (c) run(c);
            }
          }}
          placeholder="Search symbol or page…  (Cmd/Ctrl+K)"
          className="w-full border-b border-zinc-800 bg-transparent px-4 py-3 text-sm text-zinc-100 outline-none placeholder:text-zinc-500"
        />
        <ul className="max-h-80 overflow-y-auto py-1">
          {results.length === 0 ? (
            <li className="px-4 py-3 text-sm text-zinc-500">No results</li>
          ) : (
            results.map((c, i) => (
              <li key={c.kind === "symbol" ? `s-${c.symbol}` : `p-${c.view}`}>
                <button
                  onMouseEnter={() => setSel(i)}
                  onClick={() => run(c)}
                  className={`flex w-full items-center gap-3 px-4 py-2 text-left text-sm ${
                    i === sel ? "bg-zinc-800 text-zinc-100" : "text-zinc-300"
                  }`}
                >
                  <span className="opacity-70">{c.kind === "symbol" ? "📈" : "📄"}</span>
                  {c.kind === "symbol" ? (
                    <>
                      <span className="font-medium">{c.symbol}</span>
                      <span className="ml-auto text-xs text-zinc-500">go to symbol</span>
                    </>
                  ) : (
                    <>
                      <span>{c.label}</span>
                      <span className="ml-auto text-xs text-zinc-500">page</span>
                    </>
                  )}
                </button>
              </li>
            ))
          )}
        </ul>
      </div>
    </div>
  );
}
