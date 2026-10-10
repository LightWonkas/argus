"""Brain assembly factory — builds the full analysis pipeline from settings.

Picks **Claude** (Anthropic API) or **Ollama** (local, $0) per role from the settings; both
fit the same :class:`LLMClient` seam → the router/analyst/Copilot stay unchanged. CostGuard +
LLMRouter are shared. Provider selection lives in one place (`build_router`) — the analyst
and Copilot share it.
"""

from __future__ import annotations

from argus.brain.analyst import TechnicalAnalyst
from argus.brain.budget import CostGuard, CostStore
from argus.brain.llm import LIVE_PROVIDERS, LLMClient, Provider
from argus.brain.router import LLMRouter, RoleBinding, TaskTier
from argus.config.settings import Settings, get_settings

# Role → that role's model attribute (on Settings). Provider: `settings.provider_for(role)`.
_ROLE_MODEL_ATTR: dict[TaskTier, str] = {
    TaskTier.ROUTINE: "model_routine",
    TaskTier.DEEP: "model_deep",
    TaskTier.COPILOT: "model_copilot",
    TaskTier.NEWS_ROUTER: "model_news_router",
    TaskTier.EXCHANGE_EXPERT: "model_exchange_expert",
}


def build_llm_client(settings: Settings, provider: str | None = None) -> LLMClient:
    """LLM client for the given provider (claude | ollama). Lazy import.

    When ``provider`` is None, falls back to the global ``settings.llm_provider`` (backward
    compatibility). There is no OpenAI/OpenRouter runtime client yet → selecting one raises a
    clear ``NotImplementedError`` ("coming soon" on the Models page).
    """
    name = (provider or settings.llm_provider).strip().lower()
    if name == Provider.OLLAMA:
        from argus.brain.ollama_client import OllamaClient

        return OllamaClient(base_url=settings.ollama_base_url)

    if name == Provider.CLAUDE:
        from argus.brain.claude_client import ClaudeClient

        key = settings.anthropic_api_key
        return ClaudeClient(api_key=key.get_secret_value() if key else None)

    if name in (Provider.OPENAI, Provider.OPENROUTER):
        raise NotImplementedError(
            f"Provider '{name}' is not active yet (coming soon). Choose claude or ollama for now."
        )
    raise ValueError(f"Unknown LLM provider: {name!r}")


def build_router(settings: Settings, *, cost_store: CostStore | None = None) -> LLMRouter:
    """Per-role provider + model → :class:`LLMRouter` (shared by analyst & Copilot).

    Each role picks its provider independently; roles using the same provider **share a
    single client instance** (cache). One ``CostGuard`` protects the daily budget of all roles.
    ``cost_store``: when given, spend is written to the SQLite ledger and the cap is read
    across processes (wired by the interface/api factories; tests pass None → in-memory).
    """
    clients: dict[str, LLMClient] = {}  # provider name → single client (cache)

    def client_for(provider: str) -> LLMClient:
        if provider not in clients:
            clients[provider] = build_llm_client(settings, provider)
        return clients[provider]

    bindings: dict[TaskTier, RoleBinding] = {}
    for tier, model_attr in _ROLE_MODEL_ATTR.items():
        provider = settings.provider_for(tier.value)
        model: str = getattr(settings, model_attr)
        # Backward compatibility: if an Ollama role's model is empty OR a Claude model (the
        # user switched the global provider to ollama without changing role models) → fall
        # back to the default ollama_model.
        if provider == Provider.OLLAMA and (not model or model.startswith("claude-")):
            model = settings.ollama_model
        bindings[tier] = RoleBinding(client=client_for(provider), model=model, provider=provider)
    return LLMRouter(bindings, guard=CostGuard(settings.llm_daily_budget_usd, store=cost_store))


def build_analyst(
    settings: Settings | None = None, *, cost_store: CostStore | None = None
) -> TechnicalAnalyst:
    """Build a live (per-role provider) :class:`TechnicalAnalyst` from settings."""
    return TechnicalAnalyst(build_router(settings or get_settings(), cost_store=cost_store))


__all__ = ["LIVE_PROVIDERS", "build_analyst", "build_llm_client", "build_router"]
