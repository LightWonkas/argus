"""OllamaClient + provider selection (brain.ollama_client + factory) tests — offline."""

from __future__ import annotations

from typing import Any

import pytest

from argus.brain.llm import LLMMessage
from argus.brain.ollama_client import OllamaClient
from argus.config.settings import Settings
from argus.core.errors import AnalysisError


def _fake_transport(reply: str) -> Any:
    captured: dict[str, Any] = {}

    def transport(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        captured["path"] = path
        captured["payload"] = payload
        return {"message": {"content": reply}, "prompt_eval_count": 12, "eval_count": 7}

    transport.captured = captured  # type: ignore[attr-defined]
    return transport


# --- OllamaClient ---


def test_ollama_complete_returns_response() -> None:
    t = _fake_transport("local reply")
    client = OllamaClient(base_url="http://localhost:11434", transport=t)
    resp = client.complete(
        system="system",
        messages=[LLMMessage(role="user", content="hello")],
        model="llama3.2",
    )
    assert resp.text == "local reply"
    assert resp.model == "llama3.2"
    assert resp.cost_usd == 0.0  # local → free
    assert resp.input_tokens == 12
    assert resp.output_tokens == 7


def test_ollama_sends_system_then_messages() -> None:
    t = _fake_transport("ok")
    OllamaClient(transport=t).complete(
        system="SYS",
        messages=[LLMMessage(role="user", content="question")],
        model="qwen2.5",
    )
    msgs = t.captured["payload"]["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1] == {"role": "user", "content": "question"}
    assert t.captured["payload"]["stream"] is False
    assert t.captured["path"] == "/api/chat"


def test_ollama_network_error_wrapped() -> None:
    def broken(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionError("ollama is down")

    with pytest.raises(AnalysisError, match="Ollama call failed"):
        OllamaClient(transport=broken).complete(system="x", messages=[], model="m")


# --- Provider selection (factory) ---


def test_factory_selects_ollama() -> None:
    from argus.brain.factory import build_llm_client

    cfg = Settings(llm_provider="ollama", ollama_base_url="http://ollama.test:11434")
    client = build_llm_client(cfg)
    assert isinstance(client, OllamaClient)


def test_factory_selects_claude_by_default() -> None:
    from argus.brain.claude_client import ClaudeClient
    from argus.brain.factory import build_llm_client

    cfg = Settings(llm_provider="claude")
    assert isinstance(build_llm_client(cfg), ClaudeClient)


def test_router_ollama_backcompat_falls_back_to_ollama_model() -> None:
    # Back-compat: global ollama + role models left at the Claude defaults → ollama_model.
    from argus.brain.factory import build_router
    from argus.brain.router import TaskTier

    cfg = Settings(llm_provider="ollama", ollama_model="phi3")
    router = build_router(cfg)
    assert router.model_for(TaskTier.ROUTINE) == "phi3"
    assert router.model_for(TaskTier.DEEP) == "phi3"


def test_chat_engine_ollama_is_not_demo() -> None:
    from argus.brain.chat import build_chat_engine

    cfg = Settings(llm_provider="ollama", ollama_model="llama3.2")
    engine = build_chat_engine(cfg)
    assert engine.demo is False  # Ollama is real (local), not demo


def test_chat_engine_demo_when_claude_without_key() -> None:
    from argus.brain.chat import build_chat_engine

    cfg = Settings(llm_provider="claude")  # no key
    assert build_chat_engine(cfg).demo is True
