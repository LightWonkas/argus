// Shared widget chrome — every cockpit widget lives inside it (uniform title bar,
// drag handle, optional header-right controls, close). RGL uses this header as the handle
// (`.widget-drag`); mousedown is stopped on the controls so clicking them never starts a drag.

export function WidgetFrame({
  title,
  subtitle,
  icon,
  onClose,
  headerRight,
  children,
}: {
  title: string;
  subtitle?: string;
  icon?: string;
  onClose?: () => void;
  headerRight?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex h-full flex-col overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900 shadow-sm">
      <div className="widget-drag flex shrink-0 cursor-move select-none items-center gap-2 border-b border-zinc-800 bg-zinc-900/80 px-3 py-1.5">
        {icon && <span className="text-xs opacity-70">{icon}</span>}
        <span className="truncate text-xs font-semibold tracking-wide text-zinc-200">
          {title}
        </span>
        {subtitle && (
          <span className="truncate text-[10px] text-zinc-500">{subtitle}</span>
        )}
        <div
          className="ml-auto flex items-center gap-1"
          onMouseDown={(e) => e.stopPropagation()}
        >
          {headerRight}
          {onClose && (
            <button
              onClick={onClose}
              title="Remove widget"
              className="rounded px-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200"
            >
              ✕
            </button>
          )}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </div>
  );
}
