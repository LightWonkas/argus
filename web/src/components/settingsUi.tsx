// Shared form parts for the Settings/Models pages — Section (Save + result) and
// SecretField (write-only secret: "Set ✓ …AB12 / Change"). A secret is never shown back.

import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { ApiError, type SecretStatus, type SettingsWriteResult } from "../api";

export const input =
  "w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm outline-none focus:border-blue-500";
export const label = "block text-xs text-zinc-400 mb-1";

/** A form section: title + content + Save + result notice.
 *
 *  ``validate`` (if given) runs before saving; if it returns a message the save is
 *  **cancelled** and the message is shown as an error (invalid data never reaches the API —
 *  empty symbols etc. are caught honestly). Returning null/undefined means the data is valid. */
export function Section({
  title,
  onSave,
  validate,
  children,
}: {
  title: string;
  onSave: () => Promise<SettingsWriteResult>;
  validate?: () => string | null;
  children: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean;
    msg: string;
    restart: boolean;
  } | null>(null);

  // The success notice disappears after 4 s (toast feel); errors stay (so the user can read them).
  useEffect(() => {
    if (result?.ok && !result.restart) {
      const t = setTimeout(() => setResult(null), 4_000);
      return () => clearTimeout(t);
    }
  }, [result]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = validate?.();
    if (problem) {
      setResult({ ok: false, msg: problem, restart: false });
      return;
    }
    setBusy(true);
    setResult(null);
    try {
      const r = await onSave();
      setResult({
        ok: true,
        msg: r.message || "Saved",
        restart: r.requires_restart,
      });
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Connection error";
      setResult({ ok: false, msg, restart: false });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="rounded-xl border border-zinc-800 bg-zinc-900 p-5"
    >
      <h2 className="mb-4 text-sm font-semibold text-zinc-200">{title}</h2>
      <div className="space-y-3">{children}</div>
      <div className="mt-4 flex items-center gap-3">
        <button
          type="submit"
          disabled={busy}
          className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save"}
        </button>
        {result && (
          <span
            className={`text-xs ${result.ok ? "text-emerald-400" : "text-red-400"}`}
          >
            {result.ok ? "✓ " : "✕ "}
            {result.msg}
            {result.restart && (
              <span className="ml-1 text-amber-400">
                · Restart Argus for the change to take effect.
              </span>
            )}
          </span>
        )}
      </div>
    </form>
  );
}

/** Write-only secret field: "Set ✓ ..AB12 / Change" — sent only if changed. */
export function SecretField({
  title,
  status,
  value,
  onChange,
}: {
  title: string;
  status: SecretStatus;
  value: string | null; // null = leave untouched, "" = empty while in change mode
  onChange: (v: string | null) => void;
}) {
  const editing = value !== null;
  return (
    <div>
      <span className={label}>{title}</span>
      {!editing ? (
        <div className="flex items-center gap-2">
          <span className="text-sm text-zinc-300">
            {status.set ? `Set ✓ …${status.last4}` : "Not set"}
          </span>
          <button
            type="button"
            onClick={() => onChange("")}
            className="rounded border border-zinc-700 px-2 py-0.5 text-xs text-zinc-300 hover:bg-zinc-800"
          >
            Change
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <input
            type="password"
            autoComplete="new-password"
            value={value}
            placeholder="New value"
            onChange={(e) => onChange(e.target.value)}
            className={input}
          />
          <button
            type="button"
            onClick={() => onChange(null)}
            className="shrink-0 rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-400 hover:bg-zinc-800"
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}
