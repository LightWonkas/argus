// In-app confirm/prompt dialog — a modal matching the cockpit theme instead of the browser's
// native confirm()/prompt() windows. Enter confirms, Escape/outside click cancels.

import { useEffect, useRef, useState } from "react";

export interface DialogRequest {
  /** Question/title shown in the dialog. */
  message: string;
  /** If defined, prompt mode: a text input is shown; the value is its initial text. */
  input?: string;
  /** Confirm button label (default: "OK"). */
  confirmLabel?: string;
  /** Destructive action (delete/reset) — the confirm button is red-toned. */
  danger?: boolean;
  /** Called on confirm; in prompt mode receives the entered (trimmed) text. */
  onConfirm: (value: string) => void;
}

export function ConfirmDialog({
  request,
  onClose,
}: {
  request: DialogRequest;
  onClose: () => void;
}) {
  const isPrompt = request.input !== undefined;
  const [value, setValue] = useState(request.input ?? "");
  const inputRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (isPrompt) inputRef.current?.select();
    else confirmRef.current?.focus();
  }, [isPrompt]);

  function confirm() {
    const v = value.trim();
    if (isPrompt && !v) return; // an empty name is not accepted
    onClose();
    request.onConfirm(v);
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
        if (e.key === "Enter") confirm();
      }}
      role="dialog"
      aria-modal="true"
      aria-label={request.message}
    >
      <div
        className="w-80 rounded-lg border border-zinc-700 bg-zinc-900 p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <p className="text-sm text-zinc-200">{request.message}</p>
        {isPrompt && (
          <input
            ref={inputRef}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="mt-3 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-100 outline-none focus:border-zinc-500"
          />
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
          >
            Cancel
          </button>
          <button
            ref={confirmRef}
            onClick={confirm}
            className={`rounded-md px-3 py-1.5 text-xs font-medium ${
              request.danger
                ? "bg-red-600/80 text-white hover:bg-red-600"
                : "bg-indigo-600 text-white hover:bg-indigo-500"
            }`}
          >
            {request.confirmLabel ?? "OK"}
          </button>
        </div>
      </div>
    </div>
  );
}
