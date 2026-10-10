"""LLM provider abstraction (seam) — provider-neutral.

:class:`~argus.brain.claude_client.ClaudeClient` (Claude) and the local ``OllamaClient``
fit the **same interface**. The router picks the model; this layer only defines the
"text in → text + usage out" contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Provider(StrEnum):
    """LLM provider — each role can choose independently (Models page).

    CLAUDE/OLLAMA work today; OPENAI/OPENROUTER are selectable in the UI but have no runtime
    client yet (the factory raises a clear error). See the "coming soon" badge on the
    Models page.
    """

    CLAUDE = "claude"
    OLLAMA = "ollama"
    OPENAI = "openai"
    OPENROUTER = "openrouter"


# Live providers (with a runtime client) — the rest are UI-only ("coming soon").
LIVE_PROVIDERS: frozenset[Provider] = frozenset({Provider.CLAUDE, Provider.OLLAMA})

# Model pricing (USD / 1M tokens: input, output).
# OpenAI/OpenRouter models are added once catalogued; otherwise estimate_cost returns 0.0.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def estimate_cost(
    model: str, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0
) -> float:
    """Approximate USD cost from token usage (cache reads ~0.1× input)."""
    in_rate, out_rate = MODEL_PRICING.get(model, (0.0, 0.0))
    billable_input = max(input_tokens - cache_read_tokens, 0)
    return (
        billable_input * in_rate + cache_read_tokens * in_rate * 0.1 + output_tokens * out_rate
    ) / 1_000_000


@dataclass(frozen=True)
class LLMMessage:
    """A single conversation message (provider-neutral)."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMResponse:
    """The text of an LLM completion + its usage/cost."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    search_count: int = 0  # server-side web search count ($10/1000 — outside token cost)


class LLMClient(ABC):
    """Common interface implemented by the LLM providers (Claude/Ollama)."""

    @abstractmethod
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
        """Produce a completion for the given system + messages; returns text only."""
