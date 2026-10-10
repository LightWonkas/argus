import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, api, type ChatMessage } from "../api";

/** Argus Copilot: chat with the brain in the browser. JSON response (no streaming yet). */
export function Copilot({ contextSymbol }: { contextSymbol?: string | null }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [demo, setDemo] = useState(false);
  // If no reply could be produced, show an explicit status badge (budget/error ≠ normal reply).
  const [status, setStatus] = useState<"ok" | "budget_exceeded" | "error">("ok");
  const [err, setErr] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Load history (persistent chat).
  useEffect(() => {
    api
      .chatHistory()
      .then((h) =>
        setMessages(
          h.map((t) => ({ role: t.role === "assistant" ? "assistant" : "user", content: t.content })),
        ),
      )
      .catch(() => {});
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, busy]);

  async function send(e: FormEvent) {
    e.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    const next = [...messages, { role: "user" as const, content: text }];
    setMessages(next);
    setDraft("");
    setBusy(true);
    setErr(null);
    try {
      const r = await api.chat(next, contextSymbol);
      setDemo(r.demo);
      setStatus(r.status ?? "ok");
      setMessages([...next, { role: "assistant", content: r.reply }]);
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : "Connection error");
      setMessages(next); // the user message stays
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="flex h-full flex-col rounded-xl border border-zinc-800 bg-zinc-900">
      <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-zinc-200">Argus Copilot</span>
          {demo && (
            <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] text-amber-400">
              demo
            </span>
          )}
          {status !== "ok" && (
            <span className="rounded bg-rose-500/20 px-1.5 py-0.5 text-[10px] text-rose-400">
              {status === "budget_exceeded" ? "budget exhausted" : "not evaluated"}
            </span>
          )}
        </div>
        {contextSymbol && (
          <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-400">
            context: {contextSymbol}
          </span>
        )}
      </div>

      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {messages.length === 0 && (
          <div className="mt-6 space-y-3">
            <p className="text-center text-sm text-zinc-500">
              Suggested questions for this panel:
            </p>
            {[
              "What do the latest alerts show? Summarize.",
              `What is the technical picture for ${contextSymbol ?? "BTCUSDT"}? Explain with the evidence tier.`,
              "Which are the strongest and weakest opinions in the analysis journal?",
            ].map((p) => (
              <button
                key={p}
                onClick={() => setDraft(p)}
                className="block w-full rounded-lg border border-zinc-800 bg-zinc-800/40 px-3 py-2 text-left text-sm text-zinc-300 hover:border-zinc-700 hover:bg-zinc-800"
              >
                {p}
              </button>
            ))}
          </div>
        )}
        {messages.map((m, i) => (
          <div
            key={i}
            className={`max-w-[90%] rounded-lg px-3 py-2 text-sm ${
              m.role === "user"
                ? "ml-auto bg-blue-600/20 text-zinc-100"
                : "bg-zinc-800 text-zinc-200"
            }`}
          >
            <p className="whitespace-pre-wrap leading-relaxed">{m.content}</p>
          </div>
        ))}
        {busy && (
          <div className="max-w-[90%] rounded-lg bg-zinc-800 px-3 py-2 text-sm text-zinc-400">
            thinking…
          </div>
        )}
        {err && <p className="text-xs text-red-400">{err}</p>}
      </div>

      <form onSubmit={send} className="border-t border-zinc-800 p-3">
        <div className="flex items-end gap-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send(e);
              }
            }}
            rows={2}
            placeholder="Ask a question…"
            className="flex-1 resize-none rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-blue-500"
          />
          <button
            type="submit"
            disabled={busy || !draft.trim()}
            className="rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
          >
            Send
          </button>
        </div>
        <p className="mt-2 text-center text-[10px] text-zinc-500">
          Replies are informational · Not financial advice.
        </p>
      </form>
    </section>
  );
}
