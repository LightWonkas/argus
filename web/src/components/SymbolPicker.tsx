// Small symbol picker — used in the header bar (chart/quote). Known symbols come from /api/quotes.

export function SymbolPicker({
  value,
  symbols,
  onChange,
}: {
  value: string | null;
  symbols: string[];
  onChange: (s: string) => void;
}) {
  // Still show the active symbol if it is not in the list (edge case).
  const opts = value && !symbols.includes(value) ? [value, ...symbols] : symbols;
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value)}
      className="rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[11px] font-medium text-zinc-200 outline-none hover:border-zinc-600 focus:border-zinc-500"
    >
      {opts.length === 0 && <option value="">—</option>}
      {opts.map((s) => (
        <option key={s} value={s}>
          {s}
        </option>
      ))}
    </select>
  );
}
