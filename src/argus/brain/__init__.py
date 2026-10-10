"""L3 — intelligence / brain layer.

ai-hedge-fund style agents + an LLM router (Claude or local Ollama). Outputs follow the
analysis methodology, the evidence tiers ([A]/[B]/[C]) and the "not financial advice"
rule. Cost governor: the brain only wakes up on a trigger.

Building blocks: LLMClient seam + ClaudeClient + LLMRouter + CostGuard + TechnicalAnalyst.
Multi-agent set: the Agent interface + :func:`default_agents` (the L4 SignalAggregator
lives in ``decision``).
"""

from argus.brain.agents import Agent, AgentOpinion, AnalysisContext, default_agents
from argus.brain.analyst import TechnicalAnalyst
from argus.brain.budget import CostGuard
from argus.brain.claude_client import ClaudeClient
from argus.brain.crypto_agents import (
    CryptoExpertOpinion,
    CryptoExpertRole,
    CryptoLLMExpert,
    DeterministicCryptoExperts,
    no_data_opinion,
    synthesize_manager,
)
from argus.brain.factory import build_analyst
from argus.brain.llm import LLMClient, LLMMessage, LLMResponse, Provider, estimate_cost
from argus.brain.news_router import NewsRouter, NewsRouting, build_news_router
from argus.brain.prompts import build_system_prompt, build_user_prompt, parse_analysis
from argus.brain.router import LLMRouter, RoleBinding, TaskTier
from argus.brain.security import sanitize_external_text, wrap_external_content

__all__ = [
    "Agent",
    "AgentOpinion",
    "AnalysisContext",
    "ClaudeClient",
    "CostGuard",
    "CryptoExpertOpinion",
    "CryptoExpertRole",
    "CryptoLLMExpert",
    "DeterministicCryptoExperts",
    "LLMClient",
    "LLMMessage",
    "LLMResponse",
    "LLMRouter",
    "NewsRouter",
    "NewsRouting",
    "Provider",
    "RoleBinding",
    "TaskTier",
    "TechnicalAnalyst",
    "build_analyst",
    "build_news_router",
    "build_system_prompt",
    "build_user_prompt",
    "default_agents",
    "estimate_cost",
    "no_data_opinion",
    "parse_analysis",
    "sanitize_external_text",
    "synthesize_manager",
    "wrap_external_content",
]
