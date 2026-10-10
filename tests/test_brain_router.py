"""LLM router tests — model selection + the cost governor gate (fake LLMClient)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from argus.brain import CostGuard, LLMMessage, LLMResponse, LLMRouter, TaskTier
from argus.brain.llm import LLMClient
from argus.core.errors import BudgetExceededError


class FakeLLM(LLMClient):
    def __init__(self, *, cost: float = 0.1) -> None:
        self.calls: list[str] = []
        self._cost = cost

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        model: str,
        max_tokens: int = 8000,
        use_web_search: bool = False,
        effort: str = "high",
    ) -> LLMResponse:
        self.calls.append(model)
        return LLMResponse(
            text="ok", model=model, input_tokens=10, output_tokens=5, cost_usd=self._cost
        )


def _router(client: LLMClient, *, budget: float = 1.0) -> LLMRouter:
    return LLMRouter.from_single_client(
        client,
        model_routine="claude-sonnet-4-6",
        model_deep="claude-opus-4-8",
        guard=CostGuard(budget),
    )


def test_model_selection() -> None:
    r = _router(FakeLLM())
    assert r.model_for(TaskTier.ROUTINE) == "claude-sonnet-4-6"
    assert r.model_for(TaskTier.DEEP) == "claude-opus-4-8"


def test_complete_routes_deep_to_opus_and_records() -> None:
    fake = FakeLLM(cost=0.2)
    r = _router(fake)
    resp = r.complete(tier=TaskTier.DEEP, system="s", messages=[LLMMessage("user", "hi")])
    assert resp.model == "claude-opus-4-8"
    assert fake.calls == ["claude-opus-4-8"]


def test_budget_blocks_before_calling_client() -> None:
    fake = FakeLLM()
    r = _router(fake, budget=0.0)  # budget 0 → rejected before the first call
    with pytest.raises(BudgetExceededError):
        r.complete(tier=TaskTier.ROUTINE, system="s", messages=[LLMMessage("user", "hi")])
    assert fake.calls == []  # the client was never called (cost governor)
