"""OllamaClient — local LLM (Ollama) provider that fits the :class:`LLMClient` seam.

Free/local ($0 cost) → live testing + privacy. Talks to Ollama's ``/api/chat`` HTTP
endpoint using **stdlib ``urllib``** (no new dependency, like the Telegram sender). The
HTTP transport is **injectable** → unit tests stay offline.

Differences from Claude: no web search (`use_web_search` is ignored), `effort` is ignored,
cost is always ``0.0``. The output is still an ``LLMResponse`` (same contract) → the
router/analyst/Copilot stay unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from argus.brain.llm import LLMClient, LLMMessage, LLMResponse
from argus.core.errors import AnalysisError

# Transport: (path, body) → JSON response (dict). Defaults to a stdlib urllib POST.
Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


class OllamaClient(LLMClient):
    """LLM client that connects to a local Ollama server via ``/api/chat`` (cost $0)."""

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        transport: Transport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._post = transport or self._http_post
        self._timeout = timeout

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        model: str,
        max_tokens: int = 8000,
        use_web_search: bool = False,  # not available in Ollama — ignored
        effort: str = "high",  # not available in Ollama — ignored
    ) -> LLMResponse:
        chat_messages = [{"role": "system", "content": system}]
        chat_messages += [{"role": m.role, "content": m.content} for m in messages]
        payload = {
            "model": model,
            "messages": chat_messages,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        try:
            data = self._post("/api/chat", payload)
        except Exception as exc:  # network/server error → wrap as analysis error (like Claude)
            raise AnalysisError(f"Ollama call failed ({model} @ {self._base}): {exc}") from exc

        text = str(data.get("message", {}).get("content", ""))
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=int(data.get("prompt_eval_count", 0) or 0),
            output_tokens=int(data.get("eval_count", 0) or 0),
            cost_usd=0.0,  # local → free
        )

    def _http_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST via stdlib urllib (lazy import; only used on a real call)."""
        import json
        import urllib.request

        req = urllib.request.Request(
            self._base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            result: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            return result
