"""ClaudeClient — the Claude (anthropic SDK) implementation of :class:`~argus.brain.llm.LLMClient`.

The anthropic SDK is **lazily imported**; importing this module does not require the SDK.
A live call needs an API key + network → unit tests use a fake LLMClient.

Practices applied: prompt caching (constant system prompt), adaptive thinking + effort,
server-side web search, and reading only ``type=="text"`` blocks.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from argus.brain.llm import LLMClient, LLMMessage, LLMResponse, estimate_cost
from argus.core.errors import AnalysisError

# 'xhigh' effort is only supported on Opus 4.8/4.7 → fall back to 'high' on non-Opus models.
_OPUS_PREFIX = "claude-opus"


class ClaudeClient(LLMClient):
    """Produces completions via the Claude Messages API."""

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._client: Any | None = None  # lazy

    def _ensure_client(self) -> Any:
        if self._client is None:
            import anthropic  # lazy import

            self._client = (
                anthropic.Anthropic(api_key=self._api_key)
                if self._api_key
                else anthropic.Anthropic()
            )
        return self._client

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
        client = self._ensure_client()
        if effort == "xhigh" and not model.startswith(_OPUS_PREFIX):
            effort = "high"  # 'xhigh' is Opus 4.8/4.7 only

        # Cache the constant system prompt (prefix cache).
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        api_messages = [{"role": m.role, "content": m.content} for m in messages]
        tools = [{"type": "web_search_20260209", "name": "web_search"}] if use_web_search else None

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": api_messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
        if tools is not None:
            kwargs["tools"] = tools

        try:
            response = client.messages.create(**kwargs)
        except Exception as exc:  # network/SDK/auth error → wrap as analysis error
            raise AnalysisError(f"Claude call failed ({model}): {exc}") from exc

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        usage = response.usage
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        # Server-side web search count (usage.server_tool_use.web_search_requests).
        # Defensive access: 0 when absent (the SDK may omit the field if no search ran).
        server_tools = getattr(usage, "server_tool_use", None)
        search_count = int(getattr(server_tools, "web_search_requests", 0) or 0)

        return LLMResponse(
            text=text,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cost_usd=estimate_cost(
                model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
            ),
            search_count=search_count,
        )
