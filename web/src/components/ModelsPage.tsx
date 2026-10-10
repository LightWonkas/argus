import { useEffect, useState } from "react";
import { ApiError, api, type RoleModel, type SettingsView } from "../api";
import { Section, SecretField, input, label } from "./settingsUi";

// Active roles (actually in use) — each picks its provider + model independently.
const ACTIVE_ROLES = ["routine", "deep", "copilot", "news_router", "exchange_expert"] as const;
type ActiveRole = (typeof ACTIVE_ROLES)[number];

const ROLE_LABELS: Record<ActiveRole, string> = {
  routine: "Routine (alerts/scans)",
  deep: "Deep analysis (/analyze)",
  copilot: "Copilot (cockpit chat)",
  news_router: "News router",
  exchange_expert: "BIST expert (context filter)",
};

const PROVIDER_LABELS: Record<string, string> = {
  claude: "Claude (Anthropic)",
  ollama: "Ollama (local)",
  openai: "OpenAI",
  openrouter: "OpenRouter",
};

// Model placeholder hint per provider.
const MODEL_HINT: Record<string, string> = {
  claude: "claude-sonnet-4-6 · claude-opus-4-8",
  ollama: "llama3.2 · qwen2.5 · phi3",
  openai: "gpt-4o · gpt-4o-mini",
  openrouter: "google/gemini-2.0 · deepseek/deepseek-r1",
};

interface RoleState {
  provider: string;
  model: string;
}

/** Models — AI provider keys + the per-role provider/model mapping in one place. */
export function ModelsPage({ onBack }: { onBack: () => void }) {
  const [s, setS] = useState<SettingsView | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Write-only keys per provider (null = leave untouched).
  const [anthropicKey, setAnthropicKey] = useState<string | null>(null);
  const [openaiKey, setOpenaiKey] = useState<string | null>(null);
  const [openrouterKey, setOpenrouterKey] = useState<string | null>(null);

  const [budget, setBudget] = useState(0);
  const [ollamaUrl, setOllamaUrl] = useState("");
  const [ollamaModel, setOllamaModel] = useState("");

  // Role → provider + model.
  const [roles, setRoles] = useState<Record<ActiveRole, RoleState>>({
    routine: { provider: "claude", model: "" },
    deep: { provider: "claude", model: "" },
    copilot: { provider: "claude", model: "" },
    news_router: { provider: "claude", model: "" },
    exchange_expert: { provider: "ollama", model: "" },
  });
  const [plannedRoles, setPlannedRoles] = useState<RoleModel[]>([]);

  useEffect(() => {
    api
      .getSettings()
      .then((d) => {
        setS(d);
        setBudget(d.llm.llm_daily_budget_usd);
        setOllamaUrl(d.llm.ollama_base_url);
        setOllamaModel(d.llm.ollama_model);
        const byRole = Object.fromEntries(
          (d.llm.roles ?? []).map((r) => [r.role, r]),
        );
        const pick = (role: ActiveRole, fallbackModel: string): RoleState => ({
          provider: byRole[role]?.provider || d.llm.llm_provider,
          model: byRole[role]?.model || fallbackModel,
        });
        setRoles({
          routine: pick("routine", d.llm.model_routine),
          deep: pick("deep", d.llm.model_deep),
          copilot: pick("copilot", d.llm.model_copilot ?? d.llm.model_routine),
          // The news_router model is not a separate view field; it comes from the role row (byRole).
          news_router: pick("news_router", d.llm.model_routine),
          exchange_expert: pick("exchange_expert", ""),
        });
        setPlannedRoles((d.llm.roles ?? []).filter((r) => !r.active));
      })
      .catch((e) => setErr(e instanceof ApiError ? e.message : "Could not load"));
  }, []);

  if (err) return <p className="p-8 text-red-400">{err}</p>;
  if (!s) return <p className="p-8 text-zinc-500">Loading…</p>;

  const providers = s.llm.providers.length
    ? s.llm.providers
    : ["claude", "ollama", "openai", "openrouter"];
  const live = new Set(
    s.llm.live_providers.length ? s.llm.live_providers : ["claude", "ollama"],
  );

  // Whether a provider's key is set (for the UI warning).
  const keySet = (provider: string): boolean => {
    if (provider === "anthropic" || provider === "claude")
      return s.llm.anthropic_api_key.set;
    if (provider === "openai") return s.llm.openai_api_key.set;
    if (provider === "openrouter") return s.llm.openrouter_api_key.set;
    return true; // ollama needs no key
  };

  const setRole = (role: ActiveRole, patch: Partial<RoleState>) =>
    setRoles((prev) => ({ ...prev, [role]: { ...prev[role], ...patch } }));

  return (
    <main className="mx-auto max-w-3xl px-4 py-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Models</h1>
          <p className="text-xs text-zinc-500">
            Provider keys + provider and model selection for each agent/role.
          </p>
        </div>
        <button
          onClick={onBack}
          className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          ← Back to cockpit
        </button>
      </div>

      <div className="space-y-6">
        {/* Keys + general settings */}
        <Section
          title="Provider Keys"
          onSave={() =>
            api.saveLLM({
              anthropic_api_key: anthropicKey,
              openai_api_key: openaiKey,
              openrouter_api_key: openrouterKey,
              llm_daily_budget_usd: budget,
              ollama_base_url: ollamaUrl,
              ollama_model: ollamaModel,
            })
          }
        >
          <SecretField
            title="Anthropic (Claude) API Key"
            status={s.llm.anthropic_api_key}
            value={anthropicKey}
            onChange={setAnthropicKey}
          />
          <SecretField
            title="OpenAI API Key"
            status={s.llm.openai_api_key}
            value={openaiKey}
            onChange={setOpenaiKey}
          />
          <SecretField
            title="OpenRouter API Key"
            status={s.llm.openrouter_api_key}
            value={openrouterKey}
            onChange={setOpenrouterKey}
          />
          <p className="text-[11px] text-zinc-500">
            Ollama runs locally — no key needed. Keys are per provider; all
            roles using the same provider share the same key.
          </p>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <span className={label}>
                Daily budget (USD · paid providers only)
              </span>
              <input
                type="number"
                step="0.5"
                min="0"
                value={budget}
                onChange={(e) => setBudget(parseFloat(e.target.value) || 0)}
                className={input}
              />
            </div>
            <div>
              <span className={label}>Ollama address (local LLM)</span>
              <input
                value={ollamaUrl}
                onChange={(e) => setOllamaUrl(e.target.value)}
                className={input}
                placeholder="http://localhost:11434"
              />
            </div>
            <div>
              <span className={label}>Ollama default model</span>
              <input
                value={ollamaModel}
                onChange={(e) => setOllamaModel(e.target.value)}
                className={input}
                placeholder="llama3.2"
              />
            </div>
          </div>
        </Section>

        {/* Role → provider + model table */}
        <Section
          title="Role → Provider & Model"
          onSave={() =>
            api.saveLLM({
              provider_routine: roles.routine.provider,
              provider_deep: roles.deep.provider,
              provider_copilot: roles.copilot.provider,
              provider_news_router: roles.news_router.provider,
              provider_exchange_expert: roles.exchange_expert.provider,
              model_routine: roles.routine.model,
              model_deep: roles.deep.model,
              model_copilot: roles.copilot.model,
              model_news_router: roles.news_router.model,
              model_exchange_expert: roles.exchange_expert.model,
            })
          }
        >
          <div className="overflow-hidden rounded-lg border border-zinc-800">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-800 bg-zinc-800/40 text-left text-xs text-zinc-400">
                  <th className="px-3 py-2 font-medium">Role</th>
                  <th className="px-3 py-2 font-medium">Provider</th>
                  <th className="px-3 py-2 font-medium">Model</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {ACTIVE_ROLES.map((role) => {
                  const r = roles[role];
                  const pending = !live.has(r.provider);
                  const missingKey = !keySet(r.provider);
                  return (
                    <tr
                      key={role}
                      className="border-b border-zinc-800/60 align-top last:border-0"
                    >
                      <td className="px-3 py-2">
                        <span className="text-zinc-200">
                          {ROLE_LABELS[role]}
                        </span>
                      </td>
                      <td className="px-3 py-2">
                        <select
                          value={r.provider}
                          onChange={(e) =>
                            setRole(role, { provider: e.target.value })
                          }
                          className={`${input} w-36`}
                        >
                          {providers.map((p) => (
                            <option key={p} value={p}>
                              {PROVIDER_LABELS[p] ?? p}
                              {!live.has(p) ? " · coming soon" : ""}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="px-3 py-2">
                        <input
                          value={r.model}
                          onChange={(e) =>
                            setRole(role, { model: e.target.value })
                          }
                          className={input}
                          placeholder={MODEL_HINT[r.provider] ?? "model name"}
                        />
                      </td>
                      <td className="px-3 py-2">
                        {pending ? (
                          <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-400">
                            coming soon · will not run
                          </span>
                        ) : missingKey ? (
                          <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-400">
                            key missing
                          </span>
                        ) : (
                          <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[11px] text-emerald-400">
                            active
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {plannedRoles.map((r) => (
                  <tr
                    key={r.role}
                    className="border-b border-zinc-800/60 opacity-60 last:border-0"
                  >
                    <td className="px-3 py-2 text-zinc-400">{r.label}</td>
                    <td className="px-3 py-2 text-zinc-600">—</td>
                    <td className="px-3 py-2 text-zinc-600">—</td>
                    <td className="px-3 py-2">
                      <span className="rounded-full bg-zinc-700/40 px-2 py-0.5 text-[11px] text-zinc-400">
                        ready · not used by an agent yet
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] text-zinc-500">
            Each role picks its provider + model independently.{" "}
            <span className="text-amber-400/80">Claude and Ollama work</span>;
            OpenAI/OpenRouter are "coming soon" for now (selectable, but that
            role will not run). Planned roles are for multi-AI orchestration —
            the infrastructure is ready.
          </p>
        </Section>
      </div>

      <p className="mt-8 text-center text-xs text-zinc-600">
        Keys are write-only and never shown back · Not financial advice.
      </p>
    </main>
  );
}
