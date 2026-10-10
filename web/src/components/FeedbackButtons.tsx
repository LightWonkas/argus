import { useState } from "react";
import { api, type FeedbackVote } from "../api";

// Remember cast votes on the client (list endpoints do not return vote state — optimistic).
// Key: ref_type + ref_id. localStorage persists → the button stays filled across reloads.
const SEEN_KEY = "argus.feedback.voted";

type Voted = Record<string, boolean>; // key → useful

function loadVoted(): Voted {
  try {
    return JSON.parse(localStorage.getItem(SEEN_KEY) ?? "{}") as Voted;
  } catch {
    return {};
  }
}

function rememberVote(key: string, useful: boolean): void {
  const v = loadVoted();
  v[key] = useful;
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify(v));
  } catch {
    /* quota full → stays in memory only (not critical) */
  }
}

/** Inline 👍/👎 on an alert/proposal row. The vote goes to the backend (same store as
 *  Telegram) and is remembered on the client. Argus stays read-only: this is *user→system*
 *  feedback, not an order. If no store is attached the button shows an error, never crashes. */
export function FeedbackButtons({
  vote,
  size = "sm",
}: {
  vote: Omit<FeedbackVote, "useful">;
  size?: "sm" | "xs";
}) {
  const key = `${vote.ref_type}:${vote.ref_id}`;
  const [voted, setVoted] = useState<boolean | null>(() => loadVoted()[key] ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);

  async function send(useful: boolean) {
    if (busy) return;
    setBusy(true);
    setError(false);
    try {
      await api.sendFeedback({ ...vote, useful });
      setVoted(useful);
      rememberVote(key, useful);
    } catch {
      setError(true); // no store / network — never show a fake confirmation
    } finally {
      setBusy(false);
    }
  }

  const pad = size === "xs" ? "px-1 py-0.5 text-[11px]" : "px-1.5 py-0.5 text-xs";
  const base = `rounded transition-colors disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 ${pad}`;

  return (
    <span className="inline-flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        disabled={busy}
        onClick={() => void send(true)}
        title="Useful"
        aria-pressed={voted === true}
        className={`${base} ${
          voted === true ? "bg-emerald-500/20 text-emerald-300" : "text-zinc-500 hover:text-emerald-300"
        }`}
      >
        👍
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => void send(false)}
        title="Not useful"
        aria-pressed={voted === false}
        className={`${base} ${
          voted === false ? "bg-rose-500/20 text-rose-300" : "text-zinc-500 hover:text-rose-300"
        }`}
      >
        👎
      </button>
      {error && <span className="text-[10px] text-amber-400">could not save</span>}
    </span>
  );
}
