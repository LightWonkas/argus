// Cockpit view — tabs (multiple dashboards) + toolbar + movable widget grid.
// State persists in localStorage. Workspace-style layout in the spirit of terminal dashboards.

import { useEffect, useMemo, useState } from "react";
import type { Layout } from "react-grid-layout";
import { ConfirmDialog, type DialogRequest } from "../components/ConfirmDialog";
import { OnboardingCard } from "../components/OnboardingCard";
import { DashboardGrid } from "./DashboardGrid";
import { WIDGETS, WIDGET_ORDER } from "./registry";
import { defaultState, loadState, saveState, uid } from "./storage";
import type { DashboardState, DashboardTab, WidgetInstance, WidgetType } from "./types";

/** The free y position at the bottom of the grid for a new widget. */
function nextY(layout: Layout[]): number {
  return layout.reduce((m, l) => Math.max(m, l.y + l.h), 0);
}

export function DashboardView() {
  const [state, setState] = useState<DashboardState>(loadState);
  const [locked, setLocked] = useState(false); // default: unlocked (movable/resizable)
  const [addOpen, setAddOpen] = useState(false);
  const [dialog, setDialog] = useState<DialogRequest | null>(null); // in-app confirm/input dialog

  useEffect(() => saveState(state), [state]);

  const activeTab = useMemo(
    () => state.tabs.find((t) => t.id === state.activeTabId) ?? state.tabs[0],
    [state],
  );

  /** Transform the active tab and update the state. */
  function mutateTab(fn: (t: DashboardTab) => DashboardTab) {
    setState((s) => ({
      ...s,
      tabs: s.tabs.map((t) => (t.id === activeTab.id ? fn(t) : t)),
    }));
  }

  function addWidget(type: WidgetType) {
    setAddOpen(false);
    const meta = WIDGETS[type];
    const i = uid(type);
    mutateTab((t) => ({
      ...t,
      widgets: [...t.widgets, { i, type } as WidgetInstance],
      layout: [
        ...t.layout,
        { i, x: 0, y: nextY(t.layout), w: meta.size.w, h: meta.size.h, minW: meta.size.minW, minH: meta.size.minH },
      ],
    }));
  }

  function closeWidget(i: string) {
    mutateTab((t) => ({
      ...t,
      widgets: t.widgets.filter((w) => w.i !== i),
      layout: t.layout.filter((l) => l.i !== i),
    }));
  }

  function updateWidget(i: string, patch: Partial<WidgetInstance>) {
    mutateTab((t) => ({
      ...t,
      widgets: t.widgets.map((w) => (w.i === i ? { ...w, ...patch } : w)),
    }));
  }

  function onLayoutChange(layout: Layout[]) {
    // Persist the layout after drag/resize (only if it changed).
    mutateTab((t) =>
      JSON.stringify(t.layout) === JSON.stringify(layout) ? t : { ...t, layout },
    );
  }

  // ── Tab operations ───────────────────────────────────────────────────────
  function addTab() {
    const id = uid("tab");
    const name = `Panel ${state.tabs.length + 1}`;
    setState((s) => ({
      ...s,
      tabs: [...s.tabs, { id, name, widgets: [], layout: [] }],
      activeTabId: id,
    }));
  }

  function renameTab(id: string) {
    const cur = state.tabs.find((t) => t.id === id);
    setDialog({
      message: "Tab name",
      input: cur?.name ?? "",
      confirmLabel: "Save",
      onConfirm: (name) =>
        setState((s) => ({
          ...s,
          tabs: s.tabs.map((t) => (t.id === id ? { ...t, name } : t)),
        })),
    });
  }

  function deleteTab(id: string) {
    if (state.tabs.length <= 1) return; // keep at least one tab
    setDialog({
      message: "Delete this tab?",
      confirmLabel: "Delete",
      danger: true,
      onConfirm: () =>
        setState((s) => {
          const tabs = s.tabs.filter((t) => t.id !== id);
          return { ...s, tabs, activeTabId: tabs[0].id };
        }),
    });
  }

  function resetAll() {
    setDialog({
      message: "Reset the entire dashboard layout to the default?",
      confirmLabel: "Reset",
      danger: true,
      onConfirm: () => {
        setState(defaultState());
        setLocked(false);
      },
    });
  }

  return (
    <div className="flex h-full flex-col">
      {/* Tabs + toolbar */}
      <div className="flex shrink-0 items-center gap-1 border-b border-zinc-800 bg-zinc-950 px-3 py-1.5">
        <div className="flex items-center gap-1 overflow-x-auto">
          {state.tabs.map((t) => {
            const active = t.id === activeTab.id;
            return (
              <div
                key={t.id}
                className={`group flex items-center gap-1 rounded-md px-2.5 py-1 text-xs ${
                  active ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900"
                }`}
              >
                <button
                  onClick={() => setState((s) => ({ ...s, activeTabId: t.id }))}
                  onDoubleClick={() => renameTab(t.id)}
                  title="Double-click: rename"
                >
                  {t.name}
                </button>
                {active && state.tabs.length > 1 && (
                  <button
                    onClick={() => deleteTab(t.id)}
                    className="text-zinc-500 opacity-0 transition hover:text-red-400 group-hover:opacity-100"
                    title="Delete tab"
                  >
                    ✕
                  </button>
                )}
              </div>
            );
          })}
          <button
            onClick={addTab}
            className="rounded-md px-2 py-1 text-xs text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200"
            title="New dashboard tab"
          >
            ＋
          </button>
        </div>

        <div className="relative ml-auto flex items-center gap-1">
          <button
            onClick={() => setAddOpen((v) => !v)}
            className="rounded-md border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
          >
            ＋ Widget
          </button>
          {addOpen && (
            <>
              <div className="fixed inset-0 z-10" onClick={() => setAddOpen(false)} />
              <div className="absolute right-0 top-9 z-20 w-56 rounded-lg border border-zinc-700 bg-zinc-900 p-1 shadow-xl">
                {WIDGET_ORDER.map((type) => (
                  <button
                    key={type}
                    onClick={() => addWidget(type)}
                    className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800"
                  >
                    <span className="opacity-70">{WIDGETS[type].icon}</span>
                    {WIDGETS[type].label}
                  </button>
                ))}
              </div>
            </>
          )}
          <button
            onClick={() => setLocked((v) => !v)}
            className={`rounded-md border px-2.5 py-1 text-xs transition ${
              locked
                ? "border-amber-500/50 bg-amber-500/10 text-amber-300"
                : "border-zinc-700 text-zinc-300 hover:bg-zinc-800"
            }`}
            title={
              locked
                ? "Layout locked — unlock to move widgets"
                : "Widgets unlocked — lock to prevent accidental moves"
            }
          >
            {locked ? "🔒 Locked" : "🔓 Unlocked"}
          </button>
          <button
            onClick={resetAll}
            className="rounded-md px-2 py-1 text-xs text-zinc-500 hover:text-zinc-300"
            title="Reset layout"
          >
            ↺
          </button>
        </div>
      </div>

      {/* Grid */}
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <OnboardingCard />
        {activeTab.widgets.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-zinc-600">
            Empty dashboard — start with “＋ Widget” at the top right. Drag widgets by their
            header and resize them from the corner.
          </div>
        ) : (
          <DashboardGrid
            widgets={activeTab.widgets}
            layout={activeTab.layout}
            editMode={!locked}
            onLayoutChange={onLayoutChange}
            onCloseWidget={closeWidget}
            onUpdateWidget={updateWidget}
          />
        )}
        <p className="mt-4 text-center text-[10px] text-zinc-700">
          All output is informational · Not financial advice.
        </p>
      </div>

      {dialog && <ConfirmDialog request={dialog} onClose={() => setDialog(null)} />}
    </div>
  );
}
