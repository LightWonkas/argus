import { useState, type FormEvent } from "react";
import { ApiError, api } from "../api";

/** Login page (session cookie). The password travels only in the POST body. */
export function Login({ onSuccess }: { onSuccess: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username, password);
      onSuccess();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Connection error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-xl border border-zinc-800 bg-zinc-900 p-8 shadow-xl"
      >
        <div className="text-center">
          <div className="text-2xl font-semibold tracking-wide">Argus</div>
          <div className="mt-1 text-sm text-zinc-400">Always-on watch cockpit</div>
        </div>

        <label className="block">
          <span className="text-sm text-zinc-300">Username</span>
          <input
            type="text"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 outline-none focus:border-blue-500"
            required
          />
        </label>

        <label className="block">
          <span className="text-sm text-zinc-300">Password</span>
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 outline-none focus:border-blue-500"
            required
          />
        </label>

        {error && (
          <div className="rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-400">{error}</div>
        )}

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-lg bg-blue-600 py-2 font-medium text-white transition hover:bg-blue-500 disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="text-center text-xs text-zinc-500">
          Not financial advice. · Decision support
        </p>
      </form>
    </div>
  );
}
