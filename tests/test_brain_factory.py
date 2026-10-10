"""Brain factory tests — per-role provider + model selection (offline, lazy import).

`build_llm_client`/`build_router`: which role gets which provider + model, whether roles on
the same provider share one client instance, and whether openai/openrouter fail clearly.
"""

from __future__ import annotations

import pytest

from argus.brain.claude_client import ClaudeClient
from argus.brain.factory import build_llm_client, build_router
from argus.brain.ollama_client import OllamaClient
from argus.brain.router import TaskTier
from argus.config.settings import Settings


def test_build_client_claude_and_ollama() -> None:
    s = Settings()
    assert isinstance(build_llm_client(s, "claude"), ClaudeClient)
    assert isinstance(build_llm_client(s, "ollama"), OllamaClient)


def test_build_client_openai_not_yet_active() -> None:
    s = Settings()
    with pytest.raises(NotImplementedError, match="coming soon"):
        build_llm_client(s, "openai")
    with pytest.raises(NotImplementedError, match="coming soon"):
        build_llm_client(s, "openrouter")


def test_build_client_falls_back_to_global_provider() -> None:
    # provider=None → global llm_provider.
    s = Settings(llm_provider="ollama")
    assert isinstance(build_llm_client(s), OllamaClient)


def test_router_per_role_provider_and_model() -> None:
    # Each role has its own provider + model: deep=ollama (local), routine/copilot=claude.
    s = Settings(
        provider_deep="ollama",
        model_deep="qwen2.5",
        provider_routine="claude",
        model_routine="claude-sonnet-4-6",
        provider_copilot="claude",
        model_copilot="claude-haiku-4-5",
    )
    r = build_router(s)
    deep = r.binding_for(TaskTier.DEEP)
    routine = r.binding_for(TaskTier.ROUTINE)
    copilot = r.binding_for(TaskTier.COPILOT)
    assert deep.provider == "ollama" and deep.model == "qwen2.5"
    assert isinstance(deep.client, OllamaClient)
    assert routine.provider == "claude" and routine.model == "claude-sonnet-4-6"
    assert copilot.model == "claude-haiku-4-5"
    # Two roles on the same provider (claude) share a single client instance (cache).
    assert routine.client is copilot.client
    # A different provider (ollama) gets its own client.
    assert deep.client is not routine.client


def test_router_ollama_per_role_distinct_models() -> None:
    # Roles on Ollama are not collapsed onto one model — each role keeps its own local model.
    s = Settings(
        llm_provider="ollama",
        model_routine="llama3.2",
        model_deep="qwen2.5",
        model_copilot="phi3",
    )
    r = build_router(s)
    assert r.model_for(TaskTier.ROUTINE) == "llama3.2"
    assert r.model_for(TaskTier.DEEP) == "qwen2.5"
    assert r.model_for(TaskTier.COPILOT) == "phi3"


def test_router_openai_role_raises_at_build() -> None:
    # Choosing openai for an active role fails clearly at router build time (not at runtime).
    s = Settings(provider_deep="openai", model_deep="gpt-4o")
    with pytest.raises(NotImplementedError, match="coming soon"):
        build_router(s)
