"""Demo LLM client — an **offline**, honest reply when there is no API key (Copilot demo mode).

Lets Argus Copilot appear functional without a real model: it produces a deterministic,
English reply framed as "not financial advice" that **never fabricates analysis**. It echoes
the question and (if any) the context, and states clearly that a key is required for real
analysis. It fits the ``LLMClient`` seam → also used in tests (offline).
"""

from __future__ import annotations

from collections.abc import Sequence

from argus.brain.llm import LLMClient, LLMMessage, LLMResponse
from argus.core.contracts import NOT_ADVICE


class DemoLLMClient(LLMClient):
    """Offline fake client — returns an honest demo reply echoing the last user message."""

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
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"),
            "",
        )
        question = last_user.strip() or "(empty question)"
        text = (
            "🤖 **Demo mode** — no real LLM is connected (ANTHROPIC_API_KEY is not set).\n\n"
            f"Your question: “{question[:300]}”\n\n"
            "In this mode I do NOT perform real market analysis (nothing is fabricated). For "
            "live analysis, enter your Anthropic API key under Settings → LLM or connect a "
            "local Ollama.\n\n"
            "Data linked to the dashboard (analysis log, watchlist, news) is passed to the "
            "system as context data, not as instructions.\n\n"
            f"_{NOT_ADVICE}_"
        )
        return LLMResponse(text=text, model="demo", cost_usd=0.0)
