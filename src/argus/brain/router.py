"""LLM router — picks the model by task type + applies the cost governor.

Routine/alert work → Sonnet (or local Ollama), deep ``/analyze`` → Opus. The router is
provider-independent (it takes an :class:`~argus.brain.llm.LLMClient`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from argus.brain.budget import CostGuard
from argus.brain.llm import LLMClient, LLMMessage, LLMResponse


class TaskTier(StrEnum):
    """Task/role tier → provider + model selection."""

    ROUTINE = "routine"  # alert/routine → cheaper/faster model
    DEEP = "deep"  # deep /analyze → most capable model
    COPILOT = "copilot"  # dashboard chat (Argus Copilot)
    NEWS_ROUTER = "news_router"  # news classification/routing (first multi-AI agent)
    EXCHANGE_EXPERT = "exchange_expert"  # exchange expert (BIST context filter)


@dataclass(frozen=True)
class RoleBinding:
    """Runtime binding of a role — which client + model + provider.

    Roles using the same provider **share the same `client` instance** (the factory caches).
    """

    client: LLMClient
    model: str
    provider: str


class LLMRouter:
    """Picks provider+model by role tier, enforces the budget and routes the call.

    ``bindings`` maps role → :class:`RoleBinding`; a missing role falls back to ``ROUTINE``.
    A single ``CostGuard`` protects the shared daily budget for all roles.
    """

    def __init__(
        self,
        bindings: dict[TaskTier, RoleBinding],
        *,
        guard: CostGuard,
    ) -> None:
        if TaskTier.ROUTINE not in bindings:
            raise ValueError("LLMRouter requires a binding for at least the ROUTINE role")
        self._bindings = dict(bindings)
        self._guard = guard

    @classmethod
    def from_single_client(
        cls,
        client: LLMClient,
        *,
        model_routine: str,
        model_deep: str,
        guard: CostGuard,
        model_copilot: str | None = None,
        provider: str = "",
    ) -> LLMRouter:
        """Build a router from one client + role models (single provider; demo & tests)."""
        return cls(
            {
                TaskTier.ROUTINE: RoleBinding(client, model_routine, provider),
                TaskTier.DEEP: RoleBinding(client, model_deep, provider),
                TaskTier.COPILOT: RoleBinding(client, model_copilot or model_routine, provider),
                TaskTier.NEWS_ROUTER: RoleBinding(client, model_routine, provider),
                TaskTier.EXCHANGE_EXPERT: RoleBinding(client, model_routine, provider),
            },
            guard=guard,
        )

    def binding_for(self, tier: TaskTier) -> RoleBinding:
        return self._bindings.get(tier, self._bindings[TaskTier.ROUTINE])

    def model_for(self, tier: TaskTier) -> str:
        return self.binding_for(tier).model

    def complete(
        self,
        *,
        tier: TaskTier,
        system: str,
        messages: Sequence[LLMMessage],
        max_tokens: int = 8000,
        use_web_search: bool = False,
        effort: str = "high",
    ) -> LLMResponse:
        binding = self.binding_for(tier)
        # Cost governor: reject before calling when the budget is exhausted.
        self._guard.ensure_available()
        response = binding.client.complete(
            system=system,
            messages=messages,
            model=binding.model,
            max_tokens=max_tokens,
            use_web_search=use_web_search,
            effort=effort,
        )
        # Searches also count toward the cap ($0.01/search); tier is passed for the role split.
        self._guard.record(response.cost_usd, searches=response.search_count, role=tier.value)
        return response
