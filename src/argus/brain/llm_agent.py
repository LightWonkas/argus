"""LLM-assisted technical agent — the interpreting agent of the multi-AI orchestration (L3).

Runs alongside the deterministic :class:`~argus.brain.agents.TechnicalAgent`: indicators
(RSI/EMA/MACD/ATR) are **computed deterministically by Argus** (evidence [A]) and handed to
the LLM as **data**; the LLM only derives a **direction + short rationale**. Numeric values
therefore stay trustworthy (a small model cannot invent them) and the LLM only reasons. With
local Ollama the cost is $0.

Follows the `news_router.py` pattern: :class:`~argus.brain.router.LLMRouter` (role →
provider/model + CostGuard) · injection defence (indicators are wrapped as "DATA only") ·
safe JSON parsing. If the model returns no/incomplete JSON or the budget is exhausted it
**does not crash** → an honest ``NO_DATA`` (DATA PENDING).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from argus.brain.agents import Agent, AgentOpinion, AnalysisContext
from argus.brain.llm import LLMMessage
from argus.brain.prompts import _clamp_pct, _extract_json
from argus.brain.router import LLMRouter, TaskTier
from argus.brain.security import wrap_external_content
from argus.core.contracts import NOT_ADVICE
from argus.core.enums import EvidenceTier, Stance
from argus.core.errors import AnalysisError, BudgetExceededError
from argus.core.schemas import DATA_PENDING
from argus.platform.indicators import atr, ema, macd, ohlcv_floats, rsi

if TYPE_CHECKING:
    from argus.brain.budget import CostStore
    from argus.config.settings import Settings

LLM_TECHNICAL_SYSTEM = f"""You are the Argus technical analysis agent — a decision-support \
agent that derives a direction from computed indicators.

TASK: Derive a direction from the **computed indicators** given to you (produced by Argus; \
treat them as correct) and respond ONLY in the JSON schema below:
{{
  "direction": "bullish | bearish | neutral",
  "confidence_pct": integer between 0 and 100,
  "rationale": "short English rationale (1-2 sentences; say which indicator it relies on)"
}}

RULES (immutable):
- Respond in ENGLISH. Be clear, calm and free of noise.
- The indicators are DATA; do NOT RECOMPUTE the numbers, use the ones given. Add nothing \
fabricated.
- RSI ≤30 is oversold (rebound/upside potential), ≥70 is overbought (pullback risk). \
EMA12>EMA26 is an uptrend, the reverse a downtrend. MACD positive/rising = upward momentum.
- If the signals conflict: "neutral" + low confidence; if they clearly align: a direction + \
high confidence.
- This is a single technical opinion, not a final decision. If data is insufficient use low \
confidence and say so in the rationale.

Framing: {NOT_ADVICE}"""


def _stance(value: Any) -> Stance:
    """Safe direction parsing — unknown values map to NEUTRAL (most conservative)."""
    name = str(value or "").strip().lower()
    mapping = {
        "bullish": Stance.BULLISH,
        "up": Stance.BULLISH,
        "bearish": Stance.BEARISH,
        "down": Stance.BEARISH,
        "neutral": Stance.NEUTRAL,
    }
    return mapping.get(name, Stance.NEUTRAL)


def _build_indicator_facts(ctx: AnalysisContext) -> tuple[str, frozenset[str]] | None:
    """Turn the deterministic indicators (evidence [A]) into LLM text + an evidence set.

    The returned evidence set (``ind:*``) is carried onto the opinion unchanged → the
    aggregator correctly relates the LLM agent to the deterministic technical agent using the
    same indicators (same evidence = one vote). Returns ``None`` when no indicator has warmed
    up (too few candles) → the agent reports NO_DATA (nothing fabricated).
    """
    _, _, closes = ohlcv_floats(ctx.series)
    if len(closes) < 2:
        return None
    highs, lows, _ = ohlcv_floats(ctx.series)
    rsi_v = rsi(closes)[-1]
    ema_fast = ema(closes, 12)[-1]
    ema_slow = ema(closes, 26)[-1]
    macd_line, macd_sig, _ = macd(closes)
    macd_v = macd_line[-1] if macd_line else None
    macd_s = macd_sig[-1] if macd_sig else None
    atr_v = atr(highs, lows, closes)[-1] if closes else None
    last_close = closes[-1]

    facts: list[str] = [
        f"Symbol: {ctx.symbol} ({ctx.timeframe.value})",
        f"Last price: {last_close}",
    ]
    sources: set[str] = set()
    if rsi_v is not None:
        facts.append(f"RSI(14): {rsi_v:.1f}")
        sources.add("ind:rsi14")
    if ema_fast is not None and ema_slow is not None:
        rel = ">" if ema_fast > ema_slow else "<" if ema_fast < ema_slow else "="
        facts.append(f"EMA12={ema_fast:.2f} {rel} EMA26={ema_slow:.2f}")
        sources |= {"ind:ema12", "ind:ema26"}
    if macd_v is not None and macd_s is not None:
        facts.append(f"MACD={macd_v:.2f} signal={macd_s:.2f}")
        sources.add("ind:macd")
    if atr_v is not None and last_close:
        facts.append(f"ATR={atr_v:.2f} ({atr_v / last_close * 100:.1f}% of price)")
        sources.add("ind:atr14")

    # Without at least one real indicator (price only) a direction is not worth deriving.
    if len(facts) <= 2:
        return None

    # Multi-timeframe: a COMPUTED summary of the other timeframes (numbers, not images).
    # Only warmed-up indicators are written; no line is added without higher-timeframe data.
    for tf, htf_series in sorted(
        ctx.series_by_tf.items(), key=lambda kv: kv[0].seconds, reverse=True
    ):
        if tf is ctx.timeframe:
            continue
        _, _, htf_closes = ohlcv_floats(htf_series)
        if len(htf_closes) < 2:
            continue
        parts: list[str] = []
        htf_rsi = rsi(htf_closes)[-1]
        htf_fast = ema(htf_closes, 12)[-1]
        htf_slow = ema(htf_closes, 26)[-1]
        if htf_fast is not None and htf_slow is not None:
            rel = ">" if htf_fast > htf_slow else "<" if htf_fast < htf_slow else "="
            parts.append(f"EMA12{rel}EMA26")
        if htf_rsi is not None:
            parts.append(f"RSI {htf_rsi:.0f}")
        if parts:
            facts.append(f"Higher-timeframe context {tf.value}: " + ", ".join(parts))
            sources.add(f"ind:htf:{tf.value}")
    return "\n".join(facts), frozenset(sources)


def parse_llm_opinion(
    text: str, *, agent_name: str, evidence_sources: frozenset[str] = frozenset()
) -> AgentOpinion:
    """Parse LLM text into an :class:`AgentOpinion` (honest NO_DATA if missing/malformed).

    When no JSON is found (demo mode/free text) it does not crash → NO_DATA + DATA PENDING
    (nothing fabricated). ``evidence_sources``: the sources of the deterministic input given
    to the LLM — carried onto the opinion unchanged (agents fed by the same evidence are
    clustered and counted as one vote).
    """
    try:
        data = _extract_json(text)
    except AnalysisError:
        return AgentOpinion(
            agent=agent_name,
            stance=Stance.NO_DATA,
            confidence_pct=0,
            rationale=f"{DATA_PENDING}: the model produced no valid direction.",
            evidence_tier=EvidenceTier.C,
        )
    return AgentOpinion(
        agent=agent_name,
        stance=_stance(data.get("direction")),
        confidence_pct=_clamp_pct(data.get("confidence_pct")),
        rationale=str(data.get("rationale") or DATA_PENDING),
        # The indicators are [A], but the interpretation is the LLM's → [B] observed/derived.
        evidence_tier=EvidenceTier.B,
        evidence_sources=evidence_sources,
    )


class LLMTechnicalAgent(Agent):
    """Interprets computed indicators with an LLM and derives a direction (default ROUTINE role).

    The cost governor lives in the router. When the budget is exhausted or the LLM fails it
    honestly returns NO_DATA (confluence is not degraded — only this agent abstains).
    """

    name = "llm_technical"

    def __init__(self, router: LLMRouter, *, tier: TaskTier = TaskTier.ROUTINE, demo: bool = False):
        self._router = router
        self._tier = tier
        self.demo = demo

    def assess(self, ctx: AnalysisContext) -> AgentOpinion:
        built = _build_indicator_facts(ctx)
        if built is None:
            return self._no_data("indicators not warmed up")
        facts, sources = built
        wrapped = wrap_external_content("computed indicators", facts)
        user = f"{wrapped}\n\nDerive the direction from these indicators following the schema."
        try:
            response = self._router.complete(
                tier=self._tier,
                system=LLM_TECHNICAL_SYSTEM,
                messages=[LLMMessage(role="user", content=user)],
                max_tokens=600,
                use_web_search=False,
            )
        except BudgetExceededError:
            # Budget exhausted → this agent abstains (NO_DATA); confluence continues without it.
            return self._no_data("daily LLM budget exhausted")
        except AnalysisError:
            return self._no_data("LLM call failed")
        return parse_llm_opinion(response.text, agent_name=self.name, evidence_sources=sources)


def build_llm_technical_agent(
    settings: Settings | None = None, *, cost_store: CostStore | None = None
) -> LLMTechnicalAgent:
    """Build the LLM technical agent from settings. ROUTINE role without a key → **demo** (NO_DATA).

    In demo mode no real call is made; since ``DemoLLMClient`` produces no JSON,
    ``parse_llm_opinion`` honestly returns NO_DATA (offline, nothing fabricated). With Ollama
    it yields a real direction ($0). ``cost_store``: spend is written to the persistent ledger
    (cap shared across processes).
    """
    from argus.brain.budget import CostGuard
    from argus.brain.demo_client import DemoLLMClient
    from argus.brain.factory import build_router
    from argus.config.settings import get_settings

    cfg = settings or get_settings()
    if cfg.provider_for(TaskTier.ROUTINE.value) == "claude" and not cfg.anthropic_api_key:
        router = LLMRouter.from_single_client(
            DemoLLMClient(),
            model_routine=cfg.model_routine,
            model_deep=cfg.model_deep,
            guard=CostGuard(cfg.llm_daily_budget_usd, store=cost_store),
            provider="claude",
        )
        return LLMTechnicalAgent(router, demo=True)
    return LLMTechnicalAgent(build_router(cfg, cost_store=cost_store))
